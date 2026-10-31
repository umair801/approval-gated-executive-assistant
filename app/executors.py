"""Executors for write actions.

These are STUBS: they record a simulated side effect in the outbox and return a
confirmation. A real sender or booking integration replaces the body of one function
and keeps the same guard. The guard runs first, every time, and does not trust the caller:
it re-reads the recorded approval from the store and re-checks the payload hash.
"""
from __future__ import annotations

from .models import ProposedAction
from .store import Store, new_id, payload_hash


class ApprovalRequired(Exception):
    """Raised when a write is attempted without a valid recorded approval."""


class PayloadTampered(Exception):
    pass


class ActionExecutor:
    def __init__(self, store: Store) -> None:
        self.store = store

    def run(self, action: ProposedAction) -> dict:
        approval = self.store.get_approval(action.action_id)
        if approval is None:
            raise ApprovalRequired(f"No recorded approval for action {action.action_id}")
        current = payload_hash(action.payload)
        if not (approval["payload_hash"] == current == action.payload_hash):
            raise PayloadTampered(f"Payload changed after approval for action {action.action_id}")
        handler = getattr(self, f"_do_{action.kind}", None)
        if handler is None:
            raise ApprovalRequired(f"No executor for kind {action.kind!r}")
        return handler(action)

    def _record(self, action: ProposedAction) -> dict:
        return self.store.record_outbox(action.user_id, action.action_id, action.kind, action.payload)

    def _do_send_message(self, a: ProposedAction) -> dict:
        self._record(a)
        return {"simulated": True, "confirmation": new_id("msg"), "to": a.payload["to"],
                "note": "Stub sender. Replace with a real channel integration."}

    def _do_book_table(self, a: ProposedAction) -> dict:
        self._record(a)
        return {"simulated": True, "confirmation": new_id("bkg"), "restaurant": a.payload["restaurant_name"],
                "start": a.payload["start"], "party_size": a.payload["party_size"],
                "note": "Stub booking. Replace with a real booking integration or use the deep link."}

    def _do_pay(self, a: ProposedAction) -> dict:
        self._record(a)
        return {"simulated": True, "confirmation": new_id("pay"), "amount": a.payload["amount"],
                "currency": a.payload["currency"], "note": "Stub payment. No money moves."}
