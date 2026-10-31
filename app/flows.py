"""Service layer used by the API, the scheduler and the measurement script."""
from __future__ import annotations

import time
from datetime import datetime
from dataclasses import asdict, dataclass, field

from .context import AppContext, ensure_user
from .graph import build_graph, draft_message, propose_dinner_actions
from .llm import BudgetExceeded, LLMError, start_meter
from .logging_setup import get_logger
from .models import DinnerOption, DinnerPlan
from .net import UpstreamError
from .tools.google import NotConnected

log = get_logger("flows")


@dataclass
class FlowResult:
    intent: str
    reply: str
    ok: bool = True
    plan_id: str | None = None
    action_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _graph(ctx: AppContext):
    if "graph" not in ctx.cache:
        ctx.cache["graph"] = build_graph(ctx)
    return ctx.cache["graph"]


def run_chat(ctx: AppContext, user_id: str, text: str, forced_intent: str | None = None,
             actor: str = "owner") -> FlowResult:
    ensure_user(ctx, user_id)
    text = (text or "").strip()[:500]
    ctx.store.add_message(user_id, "user", text or "(scheduled briefing)", {"forced": forced_intent})
    ctx.store.append_audit(user_id, "flow_started", actor, None, {"forced_intent": forced_intent})
    meter = start_meter()
    t0 = time.perf_counter()
    try:
        state = _graph(ctx).invoke({"user_id": user_id, "text": text, "forced_intent": forced_intent, "trace": []})
        res = FlowResult(intent=state["intent"], reply=state["reply"], plan_id=state.get("plan_id"),
                         action_ids=state.get("action_ids", []), warnings=state.get("warnings", []),
                         trace=state.get("trace", []))
    except BudgetExceeded:
        ctx.store.append_audit(user_id, "flow_failed", actor, None, {"error": "BudgetExceeded"})
        raise
    except (LLMError, UpstreamError, NotConnected) as exc:
        log.error("flow failed", extra={"error": type(exc).__name__, "detail": str(exc)[:200]})
        ctx.store.append_audit(user_id, "flow_failed", actor, None, {"error": type(exc).__name__})
        msg = ("Your Google account is not connected yet. Connect it first." if isinstance(exc, NotConnected)
               else "I could not reach a required service. Nothing was changed. Please try again.")
        res = FlowResult(intent=forced_intent or "chat", reply=msg, ok=False)
        ctx.store.add_message(user_id, "assistant", msg, {"ok": False})
        return res
    res.latency_ms = round((time.perf_counter() - t0) * 1000, 2)
    res.llm_calls, res.tokens_in, res.tokens_out = meter.calls, meter.input_tokens, meter.output_tokens
    res.cost_usd = meter.cost_usd(ctx.settings.price_in_per_mtok, ctx.settings.price_out_per_mtok)
    ctx.store.append_audit(user_id, "flow_completed", actor, None, {
        "intent": res.intent, "latency_ms": res.latency_ms, "llm_calls": res.llm_calls,
        "tokens_in": res.tokens_in, "tokens_out": res.tokens_out, "cost_usd": res.cost_usd,
        "actions_proposed": len(res.action_ids)})
    ctx.store.add_message(user_id, "assistant", res.reply,
                          {"intent": res.intent, "plan_id": res.plan_id, "action_ids": res.action_ids})
    return res


def run_briefing(ctx: AppContext, user_id: str, actor: str = "scheduler") -> FlowResult:
    return run_chat(ctx, user_id, "Give me my morning briefing", forced_intent="briefing", actor=actor)


def run_briefing_if_due(ctx: AppContext, user_id: str, actor: str = "scheduler") -> FlowResult | None:
    """At most one scheduled briefing per local day."""
    today = ctx.clock().astimezone(ctx.tz).date()
    last = ctx.store.latest_plan(user_id, "briefing")
    # Compare against generated_at, which the flow stamps with the app clock (not the DB clock).
    if last and datetime.fromisoformat(last["data"]["generated_at"]).astimezone(ctx.tz).date() == today:
        return None
    return run_briefing(ctx, user_id, actor)


class ChoiceError(Exception):
    pass


def choose_restaurant(ctx: AppContext, user_id: str, plan_id: str, restaurant_id: str) -> list[str]:
    """Switch the dinner to another shortlisted restaurant.

    Pending proposals for the old choice are superseded (not executed), and fresh ones are proposed,
    including a re-drafted message that names the new restaurant."""
    row = ctx.store.get_plan(user_id, plan_id)
    if not row or row["kind"] != "dinner":
        raise ChoiceError("Plan not found")
    plan = DinnerPlan.model_validate(row["data"]["plan"])
    option = next((o for o in plan.options if o.restaurant.id == restaurant_id), None)
    if option is None:
        raise ChoiceError("That restaurant is not on this shortlist")
    for a in ctx.store.list_actions(user_id, status="pending", flow_id=plan_id):
        ctx.gate.supersede(user_id, a.action_id, f"Switched to {option.restaurant.name}")
    ordered = [option] + [o for o in plan.options if o.restaurant.id != restaurant_id]
    if plan.request.friend_name:
        msg, _, _ = draft_message(ctx, plan.request.friend_name, option.slot, ordered)
        plan = plan.model_copy(update={"draft_message": msg})
    ids = propose_dinner_actions(ctx, user_id, plan_id, plan, option)
    ctx.store.append_audit(user_id, "restaurant_chosen", "owner", None,
                           {"plan_id": plan_id, "restaurant_id": restaurant_id})
    return ids
