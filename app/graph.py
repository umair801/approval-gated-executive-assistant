"""LangGraph orchestrator.

   START -> planner -+-> respond_chat -> END
                     +-> memory_load -> tool_router -> compose -+-> approval -> memory_save -> END   (dinner)
                                                                +-> END                              (briefing)

planner       decides the intent (LLM, structured) and parses the request
memory_load   loads preferences and contacts
tool_router   runs READ tools only (the registry blocks anything else)
compose       turns facts into a validated plan with the LLM
approval      the ONLY node that creates write actions, and it only PROPOSES them
memory_save   stores minimal learned preferences
"""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .analysis import (detect_conflicts, find_dinner_slots, find_focus_blocks, looks_like_injection,
                       rank_emails, rank_restaurants)
from .context import AppContext
from .llm import LLMError
from .models import (BriefingPlan, ChatReply, DinnerDraft, DinnerOption, DinnerPlan, DinnerRequest,
                     IntentResult, PriorityItem, Slot)


class AgentState(TypedDict, total=False):
    user_id: str
    text: str
    forced_intent: str | None
    intent: str
    request: dict
    prefs: dict
    contacts: list
    raw: dict
    plan: dict
    plan_id: str
    action_ids: list
    reply: str
    warnings: list
    trace: list


def human_slot(dt: datetime, tz) -> str:
    return dt.astimezone(tz).strftime("%A %d %b at %H:%M")


def _ev_dict(e, tz) -> dict:
    return {"id": e.id, "title": e.title, "start": e.start.astimezone(tz).isoformat(timespec="minutes"),
            "end": e.end.astimezone(tz).isoformat(timespec="minutes"), "location": e.location,
            "attendees": e.attendees}


_BAD_IN_DRAFT = re.compile(r"(https?://|www\.|@|\b\d{7,}\b)")


def safe_message(msg: str, fallback: str) -> tuple[str, bool]:
    """A drafted message may not carry links, addresses or long numbers. Those can only come from
    untrusted text, so a draft containing them is replaced by the deterministic fallback."""
    msg = " ".join(msg.split())
    if not msg or _BAD_IN_DRAFT.search(msg):
        return fallback, True
    return msg, False


def build_graph(ctx: AppContext):
    tz = ctx.tz

    def trace(state: AgentState, name: str) -> list:
        return [*state.get("trace", []), name]

    # ---------------------------------------------------------------- planner
    def planner(state: AgentState) -> dict:
        today = ctx.clock().astimezone(tz).date()
        intent = state.get("forced_intent")
        if not intent:
            intent = ctx.llm.structured("intent", IntentResult, f"Classify: {state['text']}", {}).intent
        out: dict[str, Any] = {"intent": intent, "trace": trace(state, "planner")}
        if intent == "dinner":
            req = ctx.llm.structured("dinner_request", DinnerRequest,
                                     f"Extract the dinner request from: {state['text']}",
                                     {"today": today.isoformat(), "tz": str(tz)})
            if req.date_from < today:  # never plan in the past
                req = req.model_copy(update={"date_from": today, "date_to": max(req.date_to, today)})
            out["request"] = req.model_dump(mode="json")
        return out

    def route_after_planner(state: AgentState) -> str:
        return "respond_chat" if state["intent"] == "chat" else "memory_load"

    def respond_chat(state: AgentState) -> dict:
        r = ctx.llm.structured("chat", ChatReply, state["text"], {})
        return {"reply": r.text, "trace": trace(state, "respond_chat")}

    # ---------------------------------------------------------------- memory_load
    def memory_load(state: AgentState) -> dict:
        uid = state["user_id"]
        return {"prefs": ctx.registry.call(uid, "prefs.get"),
                "contacts": ctx.registry.call(uid, "contacts.list"),
                "trace": trace(state, "memory_load")}

    # ---------------------------------------------------------------- tool_router (reads only)
    def tool_router(state: AgentState) -> dict:
        uid, now = state["user_id"], ctx.clock()
        today = now.astimezone(tz).date()
        raw: dict[str, Any] = {}
        if state["intent"] == "briefing":
            day0 = datetime.combine(today, time(0), tz)
            raw["events"] = ctx.registry.call(uid, "calendar.list_events", start=day0, end=day0 + timedelta(days=2))
            raw["emails"] = ctx.registry.call(uid, "email.list_recent", since=now - timedelta(hours=48))
        else:
            req = DinnerRequest.model_validate(state["request"])
            start = datetime.combine(req.date_from, time(0), tz)
            end = datetime.combine(req.date_to + timedelta(days=1), time(0), tz)
            raw["events"] = ctx.registry.call(uid, "calendar.list_events", start=start, end=end)
            raw["restaurants"] = ctx.registry.call(uid, "places.search", cuisine=req.cuisine,
                                                   area=state["prefs"].get("home_area"))
        return {"raw": raw, "trace": trace(state, "tool_router")}

    # ---------------------------------------------------------------- compose
    def compose(state: AgentState) -> dict:
        return (compose_briefing if state["intent"] == "briefing" else compose_dinner)(state)

    def compose_briefing(state: AgentState) -> dict:
        now, prefs = ctx.clock(), state["prefs"]
        today = now.astimezone(tz).date()
        events, emails = state["raw"]["events"], state["raw"]["emails"]
        todays = sorted((e for e in events if e.start.astimezone(tz).date() == today), key=lambda e: e.start)
        tomorrow = [e for e in events if e.start.astimezone(tz).date() == today + timedelta(days=1)]
        conflicts = detect_conflicts(events, today, tz)
        focus = find_focus_blocks(events, today, tz)
        flagged = [e for e in emails if looks_like_injection(e)]
        ranked = rank_emails([e for e in emails if e not in flagged], prefs.get("vip_senders", []), now)
        facts = {
            "today": today.isoformat(), "preferences": {k: prefs.get(k) for k in ("vip_senders", "home_area")},
            "today_events": [_ev_dict(e, tz) for e in todays], "tomorrow_events": [_ev_dict(e, tz) for e in tomorrow],
            "conflicts": [c.model_dump(mode="json") for c in conflicts],
            "focus_blocks": [{"start": s.start.astimezone(tz).isoformat(timespec="minutes"),
                              "end": s.end.astimezone(tz).isoformat(timespec="minutes")} for s in focus],
            "ranked_emails": [{"id": e.id, "sender": e.sender, "subject": e.subject, "snippet": e.snippet,
                               "score": s, "reason": r} for e, s, r in ranked],
        }
        plan = ctx.llm.structured("briefing", BriefingPlan, "Write the morning briefing.", facts)

        # Validate against the facts. The model may rank and write, but it may not invent.
        known = {("event", e.id) for e in todays} | {("email", e.id) for e, _, _ in ranked}
        kept = [p for p in plan.priorities if (p.ref_type, p.ref_id) in known][:5]
        plan = plan.model_copy(update={
            "date": today.isoformat(),
            "priorities": [PriorityItem(ref_type=p.ref_type, ref_id=p.ref_id, reason=p.reason[:200], rank=i + 1)
                           for i, p in enumerate(kept)],
            "conflicts": conflicts, "focus_blocks": focus,
        })
        if flagged:
            plan = plan.model_copy(update={"notes": [*plan.notes, (
                f"{len(flagged)} email{'s' if len(flagged) != 1 else ''} contained instructions aimed at an AI "
                "assistant. Ignored, not summarized, nothing was done.")]})
            ctx.store.append_audit(state["user_id"], "suspicious_content_ignored", "agent", None,
                                   {"source": "email", "count": len(flagged), "ids": [e.id for e in flagged]})
        # 'overload' lists every event of the day, so it must not highlight them one by one.
        bad = {c_id for c in conflicts if c.kind != "overload" for c_id in c.event_ids}
        data = {
            "plan": plan.model_dump(mode="json"),
            "schedule": [{**_ev_dict(e, tz), "conflict": e.id in bad} for e in todays],
            "tomorrow": [_ev_dict(e, tz) for e in tomorrow],
            "emails": {e.id: {"sender": e.sender, "subject": e.subject} for e, _, _ in ranked},
            "flagged": [{"id": e.id, "sender": e.sender} for e in flagged],
            "generated_at": now.isoformat(),
        }
        plan_id = ctx.store.save_plan(state["user_id"], "briefing", data)
        lines = [plan.headline + ".", plan.summary]
        label = {("email", e.id): e.subject for e, _, _ in ranked} | {("event", e.id): e.title for e in todays}
        for p in plan.priorities:
            lines.append(f"{p.rank}. {label[(p.ref_type, p.ref_id)]}: {p.reason}")
        for c in conflicts:
            lines.append(f"Conflict: {c.note}")
        return {"plan": data, "plan_id": plan_id, "reply": "\n".join(lines), "trace": trace(state, "compose")}

    def compose_dinner(state: AgentState) -> dict:
        prefs, contacts = state["prefs"], state["contacts"]
        req = DinnerRequest.model_validate(state["request"])
        events, restaurants = state["raw"]["events"], state["raw"]["restaurants"]
        warnings: list[str] = []
        slots = find_dinner_slots(events, req.date_from, req.date_to, tz,
                                  duration_min=int(prefs.get("dinner_duration_min", 90)))
        if not slots:
            return {"reply": (f"I found no free evening between {req.date_from:%a %d %b} and {req.date_to:%a %d %b}. "
                              "Try a different day."), "plan": {}, "action_ids": [], "warnings": [],
                    "trace": trace(state, "compose")}
        ranked, w = rank_restaurants(restaurants, req, prefs)
        warnings += w
        if not ranked:
            return {"reply": "No restaurant matched your filters. Loosen the price or dietary filter.",
                    "plan": {}, "action_ids": [], "warnings": warnings, "trace": trace(state, "compose")}
        best = slots[0]
        options = [DinnerOption(restaurant=r, slot=best, score=s) for r, s in ranked]
        friend = next((c for c in contacts if req.friend_name and c["name"].lower() == req.friend_name.lower()), None)
        if req.friend_name is None:
            warnings.append("No friend was named, so no message was drafted.")
        elif friend is None:
            warnings.append(f"{req.friend_name} is not in your contacts. The message is drafted by name only.")
        elif not friend.get("handle"):
            warnings.append(f"No contact handle saved for {friend['name']}.")

        message, note_by_id = "", {}
        if req.friend_name:
            message, note_by_id, rejected = draft_message(ctx, req.friend_name, best, options)
            if rejected:
                warnings.append("The model's draft contained a link or number, so a safe template was used.")
        options = [o.model_copy(update={"note": note_by_id.get(o.restaurant.id, "")}) for o in options]
        plan = DinnerPlan(request=req, slots=slots, options=options, draft_message=message,
                          friend_handle=(friend or {}).get("handle"), warnings=warnings)
        data = {"plan": plan.model_dump(mode="json")}
        plan_id = ctx.store.save_plan(state["user_id"], "dinner", data)
        top = options[0]
        reply = (f"Dinner plan ready: {human_slot(best.start, tz)}, top pick {top.restaurant.name} "
                 f"({top.restaurant.cuisine}). I prepared " +
                 ("a message and a booking" if message else "a booking") +
                 ". Nothing is sent or booked until you approve it.")
        return {"plan": data, "plan_id": plan_id, "reply": reply, "warnings": warnings,
                "trace": trace(state, "compose")}

    def route_after_compose(state: AgentState) -> str:
        return "approval" if state["intent"] == "dinner" and state.get("plan") else END

    # ---------------------------------------------------------------- approval (propose only)
    def approval(state: AgentState) -> dict:
        plan = DinnerPlan.model_validate(state["plan"]["plan"])
        ids = propose_dinner_actions(ctx, state["user_id"], state["plan_id"], plan, plan.options[0])
        return {"action_ids": ids, "trace": trace(state, "approval")}

    # ---------------------------------------------------------------- memory_save
    def memory_save(state: AgentState) -> dict:
        plan = DinnerPlan.model_validate(state["plan"]["plan"])
        prefs = dict(state["prefs"])
        recent = [plan.options[0].restaurant.cuisine] + [c for c in prefs.get("recent_cuisines", [])
                                                         if c != plan.options[0].restaurant.cuisine]
        prefs["recent_cuisines"] = recent[:5]
        ctx.store.set_prefs(state["user_id"], prefs)
        return {"trace": trace(state, "memory_save")}

    g = StateGraph(AgentState)
    for name, fn in [("planner", planner), ("respond_chat", respond_chat), ("memory_load", memory_load),
                     ("tool_router", tool_router), ("compose", compose), ("approval", approval),
                     ("memory_save", memory_save)]:
        g.add_node(name, fn)
    g.add_edge(START, "planner")
    g.add_conditional_edges("planner", route_after_planner, {"respond_chat": "respond_chat", "memory_load": "memory_load"})
    g.add_edge("respond_chat", END)
    g.add_edge("memory_load", "tool_router")
    g.add_edge("tool_router", "compose")
    g.add_conditional_edges("compose", route_after_compose, {"approval": "approval", END: END})
    g.add_edge("approval", "memory_save")
    g.add_edge("memory_save", END)
    return g.compile()


# ---------------------------------------------------------------------------- shared helpers
def draft_message(ctx: AppContext, friend: str, slot: Slot, options: list[DinnerOption]):
    top = options[0].restaurant
    fallback = (f"Hi {friend}, are you free for dinner on {human_slot(slot.start, ctx.tz)}? "
                f"I was thinking {top.name}. Let me know!")
    facts = {"friend": friend, "best_slot": {"label": human_slot(slot.start, ctx.tz), "start": slot.start.isoformat()},
             "options": [{"id": o.restaurant.id, "name": o.restaurant.name, "cuisine": o.restaurant.cuisine,
                          "neighborhood": o.restaurant.neighborhood, "rating": o.restaurant.rating,
                          "distance_km": o.restaurant.distance_km} for o in options]}
    draft = ctx.llm.structured("dinner_draft", DinnerDraft, "Draft a short, friendly dinner invite and one-line notes.", facts)
    msg, rejected = safe_message(draft.message, fallback)
    ids = {o.restaurant.id for o in options}
    notes = {n.restaurant_id: n.note for n in draft.notes if n.restaurant_id in ids}
    return msg, notes, rejected


def propose_dinner_actions(ctx: AppContext, user_id: str, plan_id: str, plan: DinnerPlan,
                           option: DinnerOption) -> list[str]:
    """Create the pending write actions for one dinner option. Proposing never executes."""
    ids: list[str] = []
    r, s = option.restaurant, option.slot
    if plan.draft_message and plan.request.friend_name:
        a = ctx.gate.propose(
            user_id, "send_message",
            {"to": plan.friend_handle or plan.request.friend_name, "text": plan.draft_message},
            f"Send a dinner invite to {plan.request.friend_name}", flow_id=plan_id)
        ids.append(a.action_id)
    b = ctx.gate.propose(
        user_id, "book_table",
        {"restaurant_id": r.id, "restaurant_name": r.name, "start": s.start.isoformat(), "end": s.end.isoformat(),
         "party_size": plan.request.party_size, "link": r.link},
        f"Book {r.name} for {plan.request.party_size} on {human_slot(s.start, ctx.tz)}", flow_id=plan_id)
    ids.append(b.action_id)
    return ids
