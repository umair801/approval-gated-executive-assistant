"""Both flows end to end through the real LangGraph graph (mock model, fixed clock)."""
from __future__ import annotations

import pytest

from app.context import build_context
from app.config import Settings
from app.flows import ChoiceError, choose_restaurant, run_briefing, run_briefing_if_due, run_chat
from app.llm import LLMError, MockLLM
from app.models import BriefingPlan, PriorityItem, Conflict
from app.tools.registry import WriteToolBlocked

U = "u1"


def events(store, user=U):
    return [e["event"] for e in reversed(store.list_audit(user))]


# ------------------------------------------------------------------ morning briefing
def test_briefing_reads_only_and_proposes_nothing(ctx):
    res = run_briefing(ctx, U)
    assert res.ok and res.intent == "briefing" and res.action_ids == []
    assert ctx.store.list_actions(U) == [] and ctx.store.list_outbox(U) == []


def test_briefing_finds_the_conflicts_in_the_data(ctx):
    run_briefing(ctx, U)
    plan = ctx.store.latest_plan(U, "briefing")["data"]["plan"]
    kinds = {(c["kind"], tuple(c["event_ids"])) for c in plan["conflicts"]}
    assert ("overlap", ("e2", "e3")) in kinds
    assert plan["focus_blocks"], "expected at least one focus window"


def test_briefing_ranks_vip_first_and_skips_bulk_mail(ctx):
    run_briefing(ctx, U)
    data = ctx.store.latest_plan(U, "briefing")["data"]
    first = sorted(data["plan"]["priorities"], key=lambda p: p["rank"])[0]
    assert first["ref_type"] == "email" and first["ref_id"] == "m1"
    assert "m5" not in data["emails"]


def test_prompt_injection_email_is_ignored_and_flagged(ctx):
    res = run_briefing(ctx, U)
    data = ctx.store.latest_plan(U, "briefing")["data"]
    assert "m6" not in data["emails"]
    assert [f["id"] for f in data["flagged"]] == ["m6"]
    assert any("instructions aimed at an AI assistant" in n for n in data["plan"]["notes"])
    assert "suspicious_content_ignored" in events(ctx.store)
    assert res.action_ids == [] and ctx.store.list_outbox(U) == []


def test_briefing_drops_priorities_and_conflicts_the_model_invented(store, morning_clock):
    class Liar(MockLLM):
        def _briefing(self, user, facts):
            base = super()._briefing(user, facts)
            return base.model_copy(update={
                "priorities": [*base.priorities, PriorityItem(ref_type="email", ref_id="ghost", reason="x", rank=9)],
                "conflicts": [Conflict(kind="overlap", event_ids=["nope"], note="invented")]})

    ctx = build_context(Settings(), store=store, clock=morning_clock, llm=Liar())
    run_briefing(ctx, U)
    plan = ctx.store.latest_plan(U, "briefing")["data"]["plan"]
    assert all(p["ref_id"] != "ghost" for p in plan["priorities"])
    assert all(c["note"] != "invented" for c in plan["conflicts"])


def test_scheduled_briefing_runs_once_per_day(ctx, morning_clock):
    assert run_briefing_if_due(ctx, U) is not None
    assert run_briefing_if_due(ctx, U) is None
    morning_clock.advance(days=1)
    assert run_briefing_if_due(ctx, U) is not None


# ------------------------------------------------------------------ plan a dinner
def test_dinner_creates_pending_actions_and_executes_nothing(ctx):
    res = run_chat(ctx, U, "Plan dinner with Sam on Friday")
    assert res.ok and res.intent == "dinner" and len(res.action_ids) == 2
    acts = ctx.store.list_actions(U)
    assert {a.kind for a in acts} == {"send_message", "book_table"}
    assert {a.status for a in acts} == {"pending"}
    assert ctx.store.list_outbox(U) == []
    assert all(ctx.store.get_approval(a.action_id) is None for a in acts)


def test_dinner_slot_is_free_in_the_calendar(ctx):
    run_chat(ctx, U, "Plan dinner with Sam on Friday")
    plan = ctx.store.latest_plan(U, "dinner")["data"]["plan"]
    assert len(plan["options"]) == 3
    book = next(a for a in ctx.store.list_actions(U) if a.kind == "book_table")
    assert book.payload["start"].startswith("2026-10-16T20:30")  # after the 18:00-20:00 drinks + buffer
    assert book.payload["party_size"] == 2


def test_dinner_message_goes_to_the_saved_handle_and_names_the_restaurant(ctx):
    run_chat(ctx, U, "Plan dinner with Sam on Friday")
    msg = next(a for a in ctx.store.list_actions(U) if a.kind == "send_message")
    book = next(a for a in ctx.store.list_actions(U) if a.kind == "book_table")
    assert msg.payload["to"] == "sam@example.com"
    assert book.payload["restaurant_name"] in msg.payload["text"]


def test_dinner_respects_cuisine_and_dietary_requests(ctx):
    run_chat(ctx, U, "Plan a vegetarian dinner with Priya this weekend")
    plan = ctx.store.latest_plan(U, "dinner")["data"]["plan"]
    assert plan["request"]["dietary"] == ["vegetarian"]
    assert all("vegetarian" in o["restaurant"]["dietary"] for o in plan["options"])


def test_dinner_unknown_friend_warns_but_still_drafts(ctx):
    res = run_chat(ctx, U, "Plan dinner with Zed on Friday")
    assert any("Zed" in w for w in res.warnings)
    msg = next(a for a in ctx.store.list_actions(U) if a.kind == "send_message")
    assert msg.payload["to"] == "Zed"


def test_dinner_without_a_friend_only_proposes_a_booking(ctx):
    res = run_chat(ctx, U, "Plan dinner on Friday")
    assert [a.kind for a in ctx.store.list_actions(U)] == ["book_table"] and len(res.action_ids) == 1


def test_dinner_when_evening_is_blocked_proposes_nothing(ctx):
    res = run_chat(ctx, U, "Plan dinner with Sam on Saturday")  # family dinner 18:30-21:00
    assert "no free evening" in res.reply.lower()
    assert res.action_ids == [] and ctx.store.list_actions(U) == []


def test_choose_other_restaurant_supersedes_old_proposals(ctx):
    res = run_chat(ctx, U, "Plan dinner with Sam on Friday")
    plan_id = res.plan_id
    options = ctx.store.get_plan(U, plan_id)["data"]["plan"]["options"]
    other = options[1]["restaurant"]
    new_ids = choose_restaurant(ctx, U, plan_id, other["id"])
    assert len(new_ids) == 2
    by_status = {}
    for a in ctx.store.list_actions(U):
        by_status.setdefault(a.status, []).append(a)
    assert len(by_status["superseded"]) == 2 and len(by_status["pending"]) == 2
    msg = next(a for a in by_status["pending"] if a.kind == "send_message")
    assert other["name"] in msg.payload["text"]
    assert ctx.store.list_outbox(U) == []
    with pytest.raises(ChoiceError):
        choose_restaurant(ctx, U, plan_id, "not-on-the-list")


def test_unsafe_draft_with_link_is_replaced_by_template(store, morning_clock):
    class Phisher(MockLLM):
        def _dinner_draft(self, user, facts):
            d = super()._dinner_draft(user, facts)
            return d.model_copy(update={"message": "Pay here https://evil.example/pay now"})

    ctx = build_context(Settings(), store=store, clock=morning_clock, llm=Phisher())
    res = run_chat(ctx, U, "Plan dinner with Sam on Friday")
    msg = next(a for a in ctx.store.list_actions(U) if a.kind == "send_message")
    assert "http" not in msg.payload["text"]
    assert any("safe template" in w for w in res.warnings)


# ------------------------------------------------------------------ chat, failures, guard rails
def test_small_talk_gets_help_and_no_actions(ctx):
    res = run_chat(ctx, U, "hello there")
    assert res.intent == "chat" and "approval" in res.reply and res.action_ids == []


def test_model_failure_is_graceful_and_changes_nothing(store, morning_clock):
    class Broken(MockLLM):
        def structured(self, *a, **k):
            raise LLMError("down")

    ctx = build_context(Settings(), store=store, clock=morning_clock, llm=Broken())
    res = run_chat(ctx, U, "Plan dinner with Sam on Friday")
    assert res.ok is False and "Nothing was changed" in res.reply
    assert ctx.store.list_actions(U) == [] and "flow_failed" in events(ctx.store)


def test_write_tools_cannot_be_called_directly(ctx):
    for name in ("send_message", "book_table", "pay"):
        with pytest.raises(WriteToolBlocked):
            ctx.registry.call(U, name, to="x", text="y")
    assert events(ctx.store).count("write_blocked") == 3
    assert ctx.store.list_outbox(U) == []


def test_every_flow_records_latency_and_reads_in_the_audit_log(ctx):
    res = run_chat(ctx, U, "Plan dinner with Sam on Friday")
    assert res.latency_ms > 0
    ev = events(ctx.store)
    assert ev[0] == "flow_started" and ev[-1] == "flow_completed" and "tool_read" in ev
    done = next(e for e in ctx.store.list_audit(U) if e["event"] == "flow_completed")
    assert done["details"]["latency_ms"] == res.latency_ms and done["details"]["actions_proposed"] == 2


def test_approval_coverage_is_total_after_a_full_run(ctx):
    res = run_chat(ctx, U, "Plan dinner with Sam on Friday")
    for aid in res.action_ids:
        ctx.gate.approve(U, aid, "owner")
    executed = [a for a in ctx.store.list_actions(U) if a.status == "executed"]
    assert len(executed) == 2
    assert all(ctx.store.get_approval(a.action_id) for a in executed)
    assert len(ctx.store.list_outbox(U)) == 2
    assert ctx.store.verify_audit(U) == (True, None)


# ------------------------------------------------------------------ regressions found by looking at the UI
def test_only_real_conflicts_highlight_events_not_the_overload_summary(ctx):
    run_briefing(ctx, U)
    sched = {e["id"]: e["conflict"] for e in ctx.store.latest_plan(U, "briefing")["data"]["schedule"]}
    assert sched["e2"] and sched["e3"]                      # the overlapping pair
    assert not sched["e5"] and not sched["e6"] and not sched["e7"]   # lunch, workshop, check-in are fine


def test_briefing_reply_names_each_priority_once(ctx):
    res = run_briefing(ctx, U)
    line = next(l for l in res.reply.splitlines() if l.startswith("1."))
    assert line.count("Board deck: need your numbers by noon") == 1 and "VIP sender" in line
