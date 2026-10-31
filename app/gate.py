"""The approval gate. The only path from a proposal to a side effect.

Guarantees
  1. Reads run freely. propose() never executes anything.
  2. approve() records the approval, then executes through ActionExecutor, which re-checks
     the recorded approval and the payload hash before doing anything.
  3. Approvals are idempotent: approving twice, or from two threads, executes once.
  4. Every step is written to the hash-chained audit log.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Callable

from .actions import InvalidAction, validate_payload
from .executors import ActionExecutor, ApprovalRequired, PayloadTampered
from .logging_setup import get_logger
from .models import ProposedAction
from .store import Store, idempotency_key, new_id, payload_hash, utcnow

log = get_logger("gate")


class ActionNotFound(Exception):
    pass


class ActionConflict(Exception):
    """The action is not in a state that allows this operation."""


class ActionExpired(Exception):
    pass


class ApprovalGate:
    def __init__(self, store: Store, ttl_hours: int = 24, clock: Callable[[], datetime] = utcnow) -> None:
        self.store = store
        self.ttl = timedelta(hours=ttl_hours)
        self.clock = clock
        self.executor = ActionExecutor(store)

    # ------------------------------------------------------------------ propose
    def propose(self, user_id: str, kind: str, payload: dict, summary: str,
                flow_id: str | None = None, actor: str = "agent") -> ProposedAction:
        clean = validate_payload(kind, payload)
        now = self.clock()
        action = ProposedAction(
            action_id=new_id("act"), user_id=user_id, kind=kind, payload=clean,  # type: ignore[arg-type]
            payload_hash=payload_hash(clean), summary=summary[:300], flow_id=flow_id,
            idempotency_key=idempotency_key(user_id, kind, clean, flow_id),
            created_at=now, expires_at=now + self.ttl,
        )
        stored, created = self.store.insert_action(action)
        if created:
            self.store.append_audit(user_id, "action_proposed", actor, stored.action_id,
                                    {"kind": kind, "summary": stored.summary, "flow_id": flow_id})
        else:
            self.store.append_audit(user_id, "action_deduplicated", actor, stored.action_id,
                                    {"kind": kind, "status": stored.status})
        return stored

    # ------------------------------------------------------------------ approve
    def approve(self, user_id: str, action_id: str, approver: str,
                edited_text: str | None = None) -> ProposedAction:
        action = self._load(user_id, action_id)

        if action.status == "executed":
            self.store.append_audit(user_id, "approval_repeated_ignored", approver, action_id)
            return action
        if action.status != "pending":
            raise ActionConflict(f"Action is {action.status}, cannot approve")

        if self.clock() >= action.expires_at:
            self._expire(action)
            raise ActionExpired("Approval window has passed. Ask the assistant to plan again.")

        if edited_text is not None:
            action = self._apply_edit(action, approver, edited_text)

        if payload_hash(action.payload) != action.payload_hash:
            self.store.append_audit(user_id, "tamper_detected", approver, action_id)
            raise PayloadTampered("Stored payload does not match its hash")

        approval, created = self.store.record_approval(user_id, action_id, action.payload_hash, approver)
        if not created:
            # Another request already holds this approval. Wait briefly for its result.
            return self._wait_for_final(user_id, action_id)

        self.store.append_audit(user_id, "approval_recorded", approver, action_id,
                                {"approval_id": approval["approval_id"], "payload_hash": action.payload_hash})
        try:
            result = self.executor.run(action)
        except Exception as exc:  # noqa: BLE001  any failure is recorded, then re-raised
            self.store.transition_action(user_id, action_id, "pending", "failed", approver,
                                         {"error": type(exc).__name__, "detail": str(exc)[:200]})
            self.store.append_audit(user_id, "action_failed", approver, action_id,
                                    {"error": type(exc).__name__})
            log.error("action failed", extra={"action_id": action_id, "error": type(exc).__name__})
            raise
        self.store.transition_action(user_id, action_id, "pending", "executed", approver, result)
        self.store.append_audit(user_id, "action_executed", approver, action_id,
                                {"kind": action.kind, "confirmation": result.get("confirmation"),
                                 "simulated": result.get("simulated", False)})
        return self._load(user_id, action_id)

    # ------------------------------------------------------------------ reject / supersede
    def reject(self, user_id: str, action_id: str, actor: str, reason: str | None = None) -> ProposedAction:
        action = self._load(user_id, action_id)
        if action.status == "rejected":
            return action
        if action.status != "pending":
            raise ActionConflict(f"Action is {action.status}, cannot reject")
        if self.store.transition_action(user_id, action_id, "pending", "rejected", actor,
                                        {"reason": (reason or "")[:200]}):
            self.store.append_audit(user_id, "action_rejected", actor, action_id, {"reason": (reason or "")[:200]})
        return self._load(user_id, action_id)

    def supersede(self, user_id: str, action_id: str, reason: str) -> None:
        if self.store.transition_action(user_id, action_id, "pending", "superseded", "agent", {"reason": reason}):
            self.store.append_audit(user_id, "action_superseded", "agent", action_id, {"reason": reason})

    def expire_due(self, user_id: str) -> int:
        n = 0
        for a in self.store.list_actions(user_id, status="pending"):
            if self.clock() >= a.expires_at:
                self._expire(a)
                n += 1
        return n

    # ------------------------------------------------------------------ internals
    def _load(self, user_id: str, action_id: str) -> ProposedAction:
        a = self.store.get_action(user_id, action_id)
        if a is None:
            raise ActionNotFound(action_id)
        return a

    def _expire(self, a: ProposedAction) -> None:
        if self.store.transition_action(a.user_id, a.action_id, "pending", "expired", "system"):
            self.store.append_audit(a.user_id, "action_expired", "system", a.action_id)

    def _apply_edit(self, action: ProposedAction, approver: str, edited_text: str) -> ProposedAction:
        if action.kind != "send_message":
            raise InvalidAction("Only message text can be edited")
        new_payload = validate_payload("send_message", {**action.payload, "text": edited_text})
        if new_payload == action.payload:
            return action
        new_hash = payload_hash(new_payload)
        key = idempotency_key(action.user_id, action.kind, new_payload, action.flow_id)
        if not self.store.update_pending_payload(action.user_id, action.action_id, new_payload, new_hash,
                                                 action.summary, key):
            raise ActionConflict("Action changed while editing")
        self.store.append_audit(action.user_id, "action_edited", approver, action.action_id,
                                {"old_hash": action.payload_hash, "new_hash": new_hash})
        return self._load(action.user_id, action.action_id)

    def _wait_for_final(self, user_id: str, action_id: str, timeout_s: float = 3.0) -> ProposedAction:
        deadline = time.monotonic() + timeout_s
        while True:
            a = self._load(user_id, action_id)
            if a.status != "pending" or time.monotonic() >= deadline:
                self.store.append_audit(user_id, "approval_repeated_ignored", "system", action_id)
                return a
            time.sleep(0.01)
