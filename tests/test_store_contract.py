"""Behavior every Store implementation must share (runs on MemoryStore, and PostgresStore when
TEST_DATABASE_URL is set)."""
from __future__ import annotations

from datetime import timedelta

from app.store import SYSTEM_USER, utcnow
from tests.conftest import BOOK, MSG

U, V = "u1", "u2"


def test_preferences_contacts_roundtrip_and_isolation(store):
    store.set_prefs(U, {"home_area": "Riverside", "n": [1, 2]})
    store.upsert_contact(U, "Sam", "sam@example.com", "friend")
    store.upsert_contact(U, "Sam", "sam2@example.com", None)      # upsert replaces
    assert store.get_prefs(U) == {"home_area": "Riverside", "n": [1, 2]}
    assert [c["handle"] for c in store.list_contacts(U)] == ["sam2@example.com"]
    assert store.get_prefs(V) == {} and store.list_contacts(V) == []


def test_tokens_roundtrip(store):
    store.save_token(U, "google", "CIPHERTEXT", "scope-a")
    store.save_token(U, "google", "CIPHERTEXT2", "scope-a")
    assert store.get_token(U, "google") == "CIPHERTEXT2" and store.get_token(V, "google") is None


def test_plans_and_messages(store):
    p1 = store.save_plan(U, "briefing", {"n": 1})
    p2 = store.save_plan(U, "briefing", {"n": 2})
    assert store.latest_plan(U, "briefing")["plan_id"] == p2 and store.get_plan(U, p1)["data"] == {"n": 1}
    assert store.get_plan(V, p1) is None and store.latest_plan(U, "dinner") is None
    store.add_message(U, "user", "hi", {"a": 1})
    store.add_message(U, "assistant", "hello", {})
    assert [m["text"] for m in store.list_messages(U)] == ["hi", "hello"]


def test_action_filters_and_cas_transition(store, gate):
    a = gate.propose(U, "send_message", MSG, "m", flow_id="f1")
    b = gate.propose(U, "book_table", BOOK, "b", flow_id="f2")
    assert {x.action_id for x in store.list_actions(U, status="pending")} == {a.action_id, b.action_id}
    assert [x.action_id for x in store.list_actions(U, flow_id="f2")] == [b.action_id]
    assert store.transition_action(U, a.action_id, "pending", "rejected", "owner", {"reason": "x"}) is True
    assert store.transition_action(U, a.action_id, "pending", "rejected", "owner") is False   # CAS: already moved
    assert store.get_action(U, a.action_id).result == {"reason": "x"}


def test_approval_is_unique_per_action(store, gate):
    a = gate.propose(U, "send_message", MSG, "m")
    first, created1 = store.record_approval(U, a.action_id, a.payload_hash, "owner")
    second, created2 = store.record_approval(U, a.action_id, a.payload_hash, "owner")
    assert created1 is True and created2 is False and first["approval_id"] == second["approval_id"]


def test_outbox_is_unique_per_action(store, gate):
    a = gate.propose(U, "send_message", MSG, "m")
    o1 = store.record_outbox(U, a.action_id, a.kind, a.payload)
    o2 = store.record_outbox(U, a.action_id, a.kind, a.payload)
    assert o1["outbox_id"] == o2["outbox_id"] and len(store.list_outbox(U)) == 1


def test_audit_chains_are_per_user(store):
    for i in range(3):
        store.append_audit(U, "e", "owner", None, {"i": i})
    store.append_audit(V, "e", "owner", None, {"i": 9})
    assert store.verify_audit(U) == (True, None) and store.verify_audit(V) == (True, None)
    assert [e["details"]["i"] for e in store.list_audit(U)] == [2, 1, 0]       # newest first
    assert store.list_audit(U)[-1]["prev_hash"] == "0" * 64


def test_delete_user_data_removes_everything_and_leaves_a_tombstone(store, gate):
    store.touch_user(U)
    store.set_prefs(U, {"a": 1})
    store.upsert_contact(U, "Sam", "s", None)
    store.save_token(U, "google", "c", None)
    store.save_plan(U, "dinner", {"x": 1})
    store.add_message(U, "user", "hi")
    a = gate.propose(U, "send_message", MSG, "m")
    gate.approve(U, a.action_id, "owner")
    store.set_prefs(V, {"keep": True})

    counts = store.delete_user_data(U)
    assert counts["actions"] == 1 and counts["contacts"] == 1
    exp = store.export_user_data(U)
    assert exp["preferences"] == {} and exp["contacts"] == [] and exp["actions"] == [] and exp["audit_log"] == []
    assert exp["connected_providers"] == [] and exp["messages"] == [] and exp["outbox"] == []
    assert store.get_token(U, "google") is None and store.get_approval(a.action_id) is None
    assert store.get_prefs(V) == {"keep": True}                                  # other users untouched
    tomb = store.list_audit(SYSTEM_USER)[0]
    assert tomb["event"] == "user_data_deleted" and U not in str(tomb["details"])
    assert store.verify_audit(SYSTEM_USER) == (True, None)


def test_purge_stale_users_by_prefix(store):
    store.touch_user("demo-aaaa")
    store.touch_user("owner")
    gone = store.purge_stale_users("demo-", utcnow() + timedelta(hours=1))
    assert gone == ["demo-aaaa"]
    assert store.purge_stale_users("demo-", utcnow() + timedelta(hours=1)) == []


def test_ping(store):
    assert store.ping() is True


def test_ordering_is_correct_when_the_clock_does_not_advance(store, gate, monkeypatch):
    """Regression: on Windows two saves can get the same timestamp. Order must follow insertion."""
    if store.kind != "memory":
        return  # PostgreSQL stamps each row itself
    import app.store as store_module
    frozen = utcnow()
    monkeypatch.setattr(store_module, "utcnow", lambda: frozen)
    p1 = store.save_plan(U, "briefing", {"n": 1})
    p2 = store.save_plan(U, "briefing", {"n": 2})
    assert store.latest_plan(U, "briefing")["plan_id"] == p2 and p1 != p2
