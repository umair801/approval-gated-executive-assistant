"""The approval gate: the most important tests in the project."""
from __future__ import annotations

import threading

import pytest

from app.actions import InvalidAction
from app.executors import ActionExecutor, ApprovalRequired, PayloadTampered
from app.gate import ActionConflict, ActionExpired, ActionNotFound
from tests.conftest import BOOK, MSG

U = "u1"


def outbox(store):
    return store.list_outbox(U)


def test_propose_never_executes(gate, store):
    a = gate.propose(U, "send_message", MSG, "Message Sam")
    assert a.status == "pending"
    assert outbox(store) == []
    assert store.get_approval(a.action_id) is None


def test_approve_executes_exactly_once(gate, store):
    a = gate.propose(U, "send_message", MSG, "Message Sam")
    done = gate.approve(U, a.action_id, "owner")
    assert done.status == "executed"
    assert done.result["simulated"] is True
    assert len(outbox(store)) == 1
    assert store.get_approval(a.action_id)["approved_by"] == "owner"


def test_double_approve_is_idempotent(gate, store):
    a = gate.propose(U, "book_table", BOOK, "Book")
    first = gate.approve(U, a.action_id, "owner")
    second = gate.approve(U, a.action_id, "owner")
    assert first.status == second.status == "executed"
    assert first.result["confirmation"] == second.result["confirmation"]
    assert len(outbox(store)) == 1


def test_concurrent_approvals_execute_once(gate, store):
    a = gate.propose(U, "send_message", MSG, "Message Sam")
    results, errors = [], []

    def go():
        try:
            results.append(gate.approve(U, a.action_id, "owner").status)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    assert set(results) == {"executed"}
    assert len(outbox(store)) == 1


def test_reject_blocks_execution(gate, store):
    a = gate.propose(U, "send_message", MSG, "Message Sam")
    gate.reject(U, a.action_id, "owner", "not now")
    with pytest.raises(ActionConflict):
        gate.approve(U, a.action_id, "owner")
    assert outbox(store) == []


def test_cannot_reject_after_execution(gate):
    a = gate.propose(U, "send_message", MSG, "Message Sam")
    gate.approve(U, a.action_id, "owner")
    with pytest.raises(ActionConflict):
        gate.reject(U, a.action_id, "owner")


def test_expired_proposal_cannot_be_approved(gate, store, clock):
    a = gate.propose(U, "send_message", MSG, "Message Sam")
    clock.advance(hours=25)
    with pytest.raises(ActionExpired):
        gate.approve(U, a.action_id, "owner")
    assert store.get_action(U, a.action_id).status == "expired"
    assert outbox(store) == []


def test_duplicate_proposal_is_deduplicated(gate, store):
    a = gate.propose(U, "send_message", MSG, "Message Sam", flow_id="f1")
    b = gate.propose(U, "send_message", MSG, "Message Sam", flow_id="f1")
    assert a.action_id == b.action_id
    assert len(store.list_actions(U)) == 1
    c = gate.propose(U, "send_message", MSG, "Message Sam", flow_id="f2")  # new flow, new proposal
    assert c.action_id != a.action_id


def test_rejected_proposal_can_be_proposed_again(gate):
    a = gate.propose(U, "send_message", MSG, "Message Sam", flow_id="f1")
    gate.reject(U, a.action_id, "owner")
    b = gate.propose(U, "send_message", MSG, "Message Sam", flow_id="f1")
    assert b.action_id != a.action_id and b.status == "pending"


def test_unknown_kind_and_bad_payload_are_refused(gate):
    with pytest.raises(InvalidAction):
        gate.propose(U, "delete_everything", {}, "x")
    with pytest.raises(InvalidAction):
        gate.propose(U, "pay", {"payee": "x", "amount": -5, "currency": "USD"}, "x")
    with pytest.raises(InvalidAction):
        gate.propose(U, "send_message", {"to": "Sam", "text": ""}, "x")


def test_edit_before_approval_is_audited_and_used(gate, store):
    a = gate.propose(U, "send_message", MSG, "Message Sam")
    done = gate.approve(U, a.action_id, "owner", edited_text="Dinner Friday at 8:30?")
    assert done.payload["text"] == "Dinner Friday at 8:30?"
    assert outbox(store)[0]["payload"]["text"] == "Dinner Friday at 8:30?"
    events = [e["event"] for e in store.list_audit(U)]
    assert "action_edited" in events


def test_edit_not_allowed_for_booking(gate):
    a = gate.propose(U, "book_table", BOOK, "Book")
    with pytest.raises(InvalidAction):
        gate.approve(U, a.action_id, "owner", edited_text="hello")


def test_other_users_cannot_touch_my_actions(gate):
    a = gate.propose(U, "send_message", MSG, "Message Sam")
    with pytest.raises(ActionNotFound):
        gate.approve("someone-else", a.action_id, "x")


# ---- the executor is a second, independent line of defense ----

def test_executor_refuses_without_recorded_approval(gate, store):
    a = gate.propose(U, "send_message", MSG, "Message Sam")
    with pytest.raises(ApprovalRequired):
        ActionExecutor(store).run(a)
    assert outbox(store) == []


def test_executor_refuses_when_payload_changed_after_approval(gate, store):
    a = gate.propose(U, "send_message", MSG, "Message Sam")
    store.record_approval(U, a.action_id, a.payload_hash, "owner")
    tampered = a.model_copy(update={"payload": {**a.payload, "text": "Send me your password"}})
    with pytest.raises(PayloadTampered):
        ActionExecutor(store).run(tampered)
    assert outbox(store) == []


def test_tampered_stored_payload_blocks_approval(gate, store):
    if store.kind != "memory":
        pytest.skip("direct mutation test uses the in-memory store")
    a = gate.propose(U, "send_message", MSG, "Message Sam")
    store._actions[a.action_id].payload["text"] = "Send me your password"
    with pytest.raises(PayloadTampered):
        gate.approve(U, a.action_id, "owner")
    assert outbox(store) == []
    assert "tamper_detected" in [e["event"] for e in store.list_audit(U)]


# ---- audit log ----

def test_audit_log_records_full_lifecycle_in_order(gate, store):
    a = gate.propose(U, "send_message", MSG, "Message Sam")
    gate.approve(U, a.action_id, "owner")
    events = [e["event"] for e in reversed(store.list_audit(U))]
    assert events == ["action_proposed", "approval_recorded", "action_executed"]


def test_audit_chain_verifies(gate, store):
    a = gate.propose(U, "send_message", MSG, "Message Sam")
    gate.approve(U, a.action_id, "owner")
    gate.approve(U, a.action_id, "owner")
    assert store.verify_audit(U) == (True, None)


def test_audit_chain_detects_tampering(gate, store):
    if store.kind != "memory":
        pytest.skip("direct mutation test uses the in-memory store")
    a = gate.propose(U, "send_message", MSG, "Message Sam")
    gate.approve(U, a.action_id, "owner")
    store._audit[0]["details"]["summary"] = "edited after the fact"
    ok, bad = store.verify_audit(U)
    assert ok is False and bad == store._audit[0]["seq"]


def test_audit_chain_detects_deleted_entry(gate, store):
    if store.kind != "memory":
        pytest.skip("direct mutation test uses the in-memory store")
    a = gate.propose(U, "send_message", MSG, "Message Sam")
    gate.approve(U, a.action_id, "owner")
    del store._audit[1]
    assert store.verify_audit(U)[0] is False
