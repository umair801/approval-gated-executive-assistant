"""HTTP layer: the full approval journey, auth modes, isolation, limits, GDPR."""
from __future__ import annotations

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import Settings
from app.context import build_context
from app.main import create_app
from app.store import MemoryStore
from tests.conftest import FakeClock
from datetime import datetime, timezone

TOKEN = "s3cret-token"


def make(**kw):
    clock = FakeClock(datetime(2026, 10, 14, 0, 0, tzinfo=timezone.utc))
    kw.setdefault("rate_limit_per_min", 1000)
    s = Settings(**kw)
    ctx = build_context(s, store=MemoryStore(), clock=clock)
    return TestClient(create_app(s, ctx)), ctx


# ------------------------------------------------------------------ the journey (local mode)
def test_health_endpoint():
    c, _ = make()
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok" and r.json()["db"] == "up" and r.json()["llm"] == "mock"


def test_full_dinner_journey_over_http():
    c, ctx = make()
    r = c.post("/api/chat", json={"message": "Plan dinner with Sam on Friday"})
    assert r.status_code == 200 and len(r.json()["action_ids"]) == 2

    pending = c.get("/api/approvals?status=pending").json()
    assert {a["kind"] for a in pending} == {"send_message", "book_table"}
    assert ctx.store.list_outbox("owner") == []          # nothing happened yet

    for a in pending:
        done = c.post(f"/api/approvals/{a['action_id']}/approve").json()
        assert done["status"] == "executed" and done["result"]["simulated"] is True
    again = c.post(f"/api/approvals/{pending[0]['action_id']}/approve")   # idempotent
    assert again.status_code == 200 and len(ctx.store.list_outbox("owner")) == 2

    audit = c.get("/api/audit").json()
    assert audit["chain_ok"] is True
    events = [e["event"] for e in audit["entries"]]
    assert events.count("action_executed") == 2 and "approval_repeated_ignored" in events


def test_edit_message_then_approve_over_http():
    c, ctx = make()
    c.post("/api/chat", json={"message": "Plan dinner with Sam on Friday"})
    msg = next(a for a in c.get("/api/approvals").json() if a["kind"] == "send_message")
    r = c.post(f"/api/approvals/{msg['action_id']}/approve", json={"edited_text": "See you Friday at 8:30!"})
    assert r.json()["payload"]["text"] == "See you Friday at 8:30!"
    assert ctx.store.list_outbox("owner")[0]["payload"]["text"] == "See you Friday at 8:30!"


def test_reject_then_approve_is_a_conflict_and_nothing_runs():
    c, ctx = make()
    c.post("/api/chat", json={"message": "Plan dinner with Sam on Friday"})
    a = c.get("/api/approvals").json()[0]
    assert c.post(f"/api/approvals/{a['action_id']}/reject", json={"reason": "no"}).json()["status"] == "rejected"
    r = c.post(f"/api/approvals/{a['action_id']}/approve")
    assert r.status_code == 409 and ctx.store.list_outbox("owner") == []


def test_error_codes():
    c, _ = make()
    assert c.post("/api/approvals/act_missing/approve").status_code == 404
    assert c.post("/api/chat", json={"message": ""}).status_code == 422
    assert c.post("/api/dinner/plan_x/choose", json={"restaurant_id": "r1"}).status_code == 404


def test_briefing_endpoints():
    c, _ = make()
    r = c.post("/api/briefing/run").json()
    assert r["intent"] == "briefing" and r["action_ids"] == []
    assert c.get("/api/briefing/latest").json()["plan"]["plan"]["conflicts"]


def test_choose_alternative_over_http():
    c, _ = make()
    res = c.post("/api/chat", json={"message": "Plan dinner with Sam on Friday"}).json()
    opts = c.get(f"/api/dinner/{res['plan_id']}").json()["plan"]["options"]
    r = c.post(f"/api/dinner/{res['plan_id']}/choose", json={"restaurant_id": opts[2]["restaurant"]["id"]})
    assert r.status_code == 200 and len(r.json()["action_ids"]) == 2
    statuses = sorted(a["status"] for a in c.get("/api/approvals").json())
    assert statuses == ["pending", "pending", "superseded", "superseded"]


# ------------------------------------------------------------------ private mode
def test_private_mode_requires_login():
    c, _ = make(access_token=TOKEN)
    assert c.get("/health").status_code == 200                       # open
    assert c.get("/api/approvals").status_code == 401
    assert c.get("/docs").status_code == 401
    r = c.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.get("/api/approvals", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/api/approvals", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_login_flow_sets_httponly_cookie():
    c, _ = make(access_token=TOKEN)
    bad = c.post("/login", data={"token": "nope"})
    assert bad.status_code == 200 and "not valid" in bad.text
    ok = c.post("/login", data={"token": TOKEN}, follow_redirects=False)
    assert ok.status_code == 303
    cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert c.get("/api/approvals").status_code == 200                # cookie is now stored by the client


def test_cross_origin_post_is_refused():
    c, _ = make()
    r = c.post("/api/chat", json={"message": "hi"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_security_headers_present():
    c, _ = make()
    h = c.get("/").headers
    assert "script-src 'self'" in h["content-security-policy"] and h["x-frame-options"] == "DENY"
    assert h["x-content-type-options"] == "nosniff"


def test_rate_limit_returns_429_with_retry_after():
    c, _ = make(rate_limit_per_min=3)
    codes = [c.post("/api/briefing/run").status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3] == 429
    assert c.post("/api/briefing/run").headers["retry-after"] == "30"


def test_login_attempts_are_rate_limited():
    c, _ = make(access_token=TOKEN)
    codes = [c.post("/login", data={"token": "x"}).status_code for _ in range(12)]
    assert 429 in codes


# ------------------------------------------------------------------ cron
def test_cron_endpoint_is_token_protected_and_idempotent():
    c, _ = make(access_token=TOKEN, cron_token="cron-1")
    assert c.post("/api/jobs/briefing").status_code == 403
    assert c.post("/api/jobs/briefing", headers={"Authorization": "Bearer nope"}).status_code == 403
    h = {"Authorization": "Bearer cron-1"}
    assert c.post("/api/jobs/briefing", headers=h).json()["ran"] is True
    assert c.post("/api/jobs/briefing", headers=h).json()["ran"] is False


def test_cron_disabled_without_token():
    c, _ = make(access_token=TOKEN)
    assert c.post("/api/jobs/briefing", headers={"Authorization": "Bearer anything"}).status_code == 403


# ------------------------------------------------------------------ demo mode
def test_demo_visitors_are_isolated_from_each_other():
    c1, ctx = make(allow_public_demo=True)
    c2 = TestClient(create_app(ctx.settings, ctx))
    c1.post("/api/chat", json={"message": "Plan dinner with Sam on Friday"})
    assert len(c1.get("/api/approvals").json()) == 2
    assert c2.get("/api/approvals").json() == []
    c1_cookie = c1.cookies.get("ea_uid")
    assert c1_cookie.startswith("demo-") and c1_cookie != c2.cookies.get("ea_uid")


def test_demo_mode_never_exposes_google_or_cron():
    c, _ = make(allow_public_demo=True, data_mode="google", google_client_id="a", google_client_secret="b",
                token_encryption_key=Fernet.generate_key().decode())
    assert c.get("/auth/google/start", follow_redirects=False).status_code == 404
    assert c.post("/api/jobs/briefing", headers={"Authorization": "Bearer x"}).status_code == 403
    assert c.get("/health").json()["data_mode"] == "sample"


def test_forged_demo_cookie_is_replaced():
    c, _ = make(allow_public_demo=True)
    c.get("/", headers={"Cookie": "ea_uid=owner"})          # a visitor tries to become the owner
    assert c.cookies.get("ea_uid").startswith("demo-")      # the server issued a fresh sandbox id instead


# ------------------------------------------------------------------ GDPR
def test_export_and_delete_my_data():
    c, ctx = make()
    c.post("/api/chat", json={"message": "Plan dinner with Sam on Friday"})
    exp = c.get("/api/me/data").json()
    assert len(exp["actions"]) == 2 and exp["contacts"] and exp["audit_log"]
    deleted = c.delete("/api/me/data").json()["deleted"]
    assert deleted["actions"] == 2 and deleted["audit_entries"] > 0
    assert ctx.store.list_actions("owner") == [] and ctx.store.get_prefs("owner") == {}
    tomb = ctx.store.list_audit("_system")[0]
    assert tomb["event"] == "user_data_deleted" and "owner" not in str(tomb["details"])


def test_delete_through_the_ui_requires_confirmation():
    c, ctx = make()
    c.post("/api/chat", json={"message": "Plan dinner with Sam on Friday"})
    c.post("/ui/delete-my-data", data={}, follow_redirects=False)
    assert len(ctx.store.list_actions("owner")) == 2          # not confirmed, nothing deleted
    c.post("/ui/delete-my-data", data={"confirm": "yes"}, follow_redirects=False)
    assert ctx.store.list_actions("owner") == []


def test_stale_demo_sandboxes_are_purged():
    from app.scheduler import purge_demo_users
    c, ctx = make(allow_public_demo=True)
    c.post("/api/chat", json={"message": "Plan dinner with Sam on Friday"})
    uid = c.cookies.get("ea_uid")
    assert purge_demo_users(ctx) == []                        # fresh, kept
    ctx.clock.advance(hours=25)
    ctx.store._users[uid]["last_seen"] = ctx.clock() - __import__("datetime").timedelta(hours=25)
    assert purge_demo_users(ctx) == [uid]
    assert ctx.store.list_actions(uid) == []


def test_security_headers_are_also_on_denied_and_limited_responses():
    c, _ = make(access_token=TOKEN, rate_limit_per_min=1)
    denied = c.get("/api/approvals")                       # 401
    assert denied.status_code == 401 and "content-security-policy" in denied.headers
    assert denied.headers["x-frame-options"] == "DENY"
    c.post("/login", data={"token": "x"})
    limited = c.post("/login", data={"token": "x"})        # 429 from the global limiter
    assert limited.status_code == 429 and "x-content-type-options" in limited.headers
