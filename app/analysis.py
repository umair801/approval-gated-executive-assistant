"""Deterministic analysis. Facts are computed here, never by the LLM.

The LLM only writes prose and ranks from these facts, and its output is checked against them.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .models import (CalendarEvent, Conflict, DinnerRequest, EmailSummary, Restaurant, Slot)

PRIORITY_WORDS = ("urgent", "asap", "deadline", "due today", "due", "by noon", "today", "invoice",
                  "action required", "need your")
LOW_WORDS = ("newsletter", "digest", "unsubscribe", "promotion", "sale")


# ----------------------------------------------------------------------------- conflicts
def detect_conflicts(events: list[CalendarEvent], day: date, tz: ZoneInfo,
                     back_to_back_gap_min: int = 10, overload_min: int = 360) -> list[Conflict]:
    todays = sorted(
        (e for e in events if not e.all_day and e.start.astimezone(tz).date() == day),
        key=lambda e: (e.start, e.end),
    )
    out: list[Conflict] = []
    for a, b in zip(todays, todays[1:]):
        if b.start < a.end:
            out.append(Conflict(kind="overlap", event_ids=[a.id, b.id],
                                note=f"'{a.title}' and '{b.title}' overlap."))
        elif b.start - a.end < timedelta(minutes=back_to_back_gap_min):
            out.append(Conflict(kind="back_to_back", event_ids=[a.id, b.id],
                                note=f"'{a.title}' ends as '{b.title}' starts. No gap to move or reset."))
    total = sum(int((e.end - e.start).total_seconds() // 60) for e in todays)
    if total > overload_min:
        out.append(Conflict(kind="overload", event_ids=[e.id for e in todays],
                            note=f"{total // 60}h{total % 60:02d}m of meetings today."))
    return out


def find_focus_blocks(events: list[CalendarEvent], day: date, tz: ZoneInfo,
                      start_h: int = 9, end_h: int = 18, min_minutes: int = 60) -> list[Slot]:
    day_start = datetime.combine(day, time(start_h), tz)
    day_end = datetime.combine(day, time(end_h), tz)
    busy = sorted(((e.start, e.end) for e in events if not e.all_day and e.end > day_start and e.start < day_end))
    cursor, out = day_start, []
    for s, e in busy:
        if s - cursor >= timedelta(minutes=min_minutes):
            out.append(Slot(start=cursor, end=s))
        cursor = max(cursor, e)
    if day_end - cursor >= timedelta(minutes=min_minutes):
        out.append(Slot(start=cursor, end=day_end))
    return out


# ----------------------------------------------------------------------------- dinner slots
def find_dinner_slots(events: list[CalendarEvent], day_from: date, day_to: date, tz: ZoneInfo,
                      duration_min: int = 90, buffer_min: int = 30, earliest: time = time(18, 30),
                      latest_start: time = time(20, 30), max_slots: int = 3) -> list[Slot]:
    """One candidate per day: the earliest 30-minute-grid start with the whole dinner (plus a
    travel buffer on each side) free."""
    busy = [(e.start, e.end) for e in events if not e.all_day]
    slots: list[Slot] = []
    d = day_from
    while d <= day_to and len(slots) < max_slots:
        cand = datetime.combine(d, earliest, tz)
        last = datetime.combine(d, latest_start, tz)
        while cand <= last:
            end = cand + timedelta(minutes=duration_min)
            lo, hi = cand - timedelta(minutes=buffer_min), end + timedelta(minutes=buffer_min)
            if not any(s < hi and e > lo for s, e in busy):
                slots.append(Slot(start=cand, end=end))
                break
            cand += timedelta(minutes=30)
        d += timedelta(days=1)
    return slots


# ----------------------------------------------------------------------------- restaurants
def rank_restaurants(candidates: list[Restaurant], req: DinnerRequest, prefs: dict,
                     top_n: int = 3) -> tuple[list[tuple[Restaurant, float]], list[str]]:
    warnings: list[str] = []
    max_price = req.max_price_level or prefs.get("max_price_level") or 4
    dietary = {d.lower() for d in (req.dietary or prefs.get("dietary", []))}
    favs = {c.lower() for c in prefs.get("favorite_cuisines", [])}
    scored: list[tuple[Restaurant, float]] = []
    for r in candidates:
        if r.price_level > max_price:
            continue
        if dietary and not dietary <= {d.lower() for d in r.dietary}:
            continue
        score = r.rating * 2 - 0.3 * r.distance_km
        if req.cuisine and r.cuisine.lower() == req.cuisine.lower():
            score += 3
        elif r.cuisine.lower() in favs:
            score += 1
        scored.append((r, round(score, 2)))
    scored.sort(key=lambda x: (-x[1], x[0].name))
    if req.cuisine and not any(r.cuisine.lower() == req.cuisine.lower() for r, _ in scored[:top_n]):
        warnings.append(f"No {req.cuisine} option fit the filters, showing the closest matches.")
    if len(scored) < top_n:
        warnings.append("Fewer than three restaurants matched your filters.")
    return scored[:top_n], warnings


# ----------------------------------------------------------------------------- email safety
_INJECTION = re.compile(
    r"(ignore (all |any )?(previous|prior|above) (instructions|messages)"
    r"|disregard (the )?(previous|above)"
    r"|\bsystem\s*:"
    r"|you are now"
    r"|reveal (your )?(system )?prompt"
    r"|(forward|send|email) (the |all |this )?(whole |entire )?.{0,40}\bto\b.{0,60}@)", re.I)


def looks_like_injection(e: EmailSummary) -> bool:
    """Flags text that reads as instructions aimed at an AI assistant. Such mail is never summarized
    or ranked, and nothing in it can trigger an action (the model has no write tools anyway)."""
    return bool(_INJECTION.search(f"{e.subject} {e.snippet}"))


# ----------------------------------------------------------------------------- email priority
def score_email(e: EmailSummary, vip_senders: list[str], now: datetime) -> tuple[int, str]:
    text = f"{e.subject} {e.snippet}".lower()
    score, reasons = 0, []
    if any(v.lower() in e.sender.lower() for v in vip_senders):
        score += 5
        reasons.append("from a VIP sender")
    hits = [w for w in PRIORITY_WORDS if w in text]
    if hits:
        score += 2 + min(len(hits), 3)
        reasons.append(f"mentions '{hits[0]}'")
    if e.unread:
        score += 1
    if now - e.received < timedelta(hours=12):
        score += 1
    if any(w in text for w in LOW_WORDS):
        score -= 4
        reasons.append("looks like bulk mail")
    return score, ", ".join(reasons) or "recent unread mail"


def rank_emails(emails: list[EmailSummary], vip_senders: list[str], now: datetime,
                top_n: int = 5, min_score: int = 3) -> list[tuple[EmailSummary, int, str]]:
    scored = [(e, *score_email(e, vip_senders, now)) for e in emails]
    scored = [x for x in scored if x[1] >= min_score]
    scored.sort(key=lambda x: (-x[1], x[0].received), reverse=False)
    return scored[:top_n]


# ----------------------------------------------------------------------------- misc
def parse_hhmm(s: str) -> time:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s.strip())
    if not m:
        raise ValueError(f"Bad time {s!r}, expected HH:MM")
    return time(int(m[1]), int(m[2]))
