from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.analysis import (detect_conflicts, find_dinner_slots, find_focus_blocks, rank_emails,
                          rank_restaurants)
from app.models import CalendarEvent, DinnerRequest
from app.tools.sample_data import (SAMPLE_PREFS, sample_emails, sample_events, sample_restaurants)

TZ = ZoneInfo("Asia/Shanghai")
WED = date(2026, 10, 14)


def ev(i, d, s, e, title=None):
    return CalendarEvent(id=i, title=title or i, start=datetime.combine(d, time(*s), TZ),
                         end=datetime.combine(d, time(*e), TZ))


def test_overlap_and_back_to_back_detected():
    events = sample_events(WED, TZ)
    kinds = [(c.kind, tuple(c.event_ids)) for c in detect_conflicts(events, WED, TZ)]
    assert ("overlap", ("e2", "e3")) in kinds          # 9:30-10:30 vs 10:00-11:00
    assert ("back_to_back", ("e1", "e2")) in kinds      # 9:30 end meets 9:30 start


def test_no_false_conflicts_on_clean_day():
    events = [ev("a", WED, (9, 0), (10, 0)), ev("b", WED, (11, 0), (12, 0))]
    assert detect_conflicts(events, WED, TZ) == []


def test_overload_flagged():
    events = [ev(f"x{i}", WED, (8 + i * 2, 0), (9 + i * 2, 55)) for i in range(4)]
    events.append(ev("y", WED, (16, 5), (18, 0)))
    assert any(c.kind == "overload" for c in detect_conflicts(events, WED, TZ, overload_min=360))


def test_conflicts_ignore_other_days():
    events = [ev("a", WED, (9, 0), (10, 0)), ev("b", WED + timedelta(days=1), (9, 30), (10, 30))]
    assert detect_conflicts(events, WED, TZ) == []


def test_focus_blocks_between_meetings():
    events = [ev("a", WED, (9, 0), (10, 0)), ev("b", WED, (14, 0), (15, 0))]
    blocks = find_focus_blocks(events, WED, TZ)
    assert [(b.start.hour, b.end.hour) for b in blocks] == [(10, 14), (15, 18)]


def test_dinner_slot_avoids_events_with_buffer():
    fri = date(2026, 10, 16)
    events = sample_events(WED, TZ)  # Friday drinks 18:00-20:00
    slots = find_dinner_slots(events, fri, fri, TZ)
    assert len(slots) == 1
    s = slots[0]
    assert s.start.time() == time(20, 30)  # 20:00 end + 30 min buffer
    for e in events:
        assert not (s.start < e.end and s.end > e.start)


def test_dinner_slot_none_when_evening_blocked():
    d = date(2026, 10, 17)
    events = [ev("fam", d, (18, 0), (23, 0))]
    assert find_dinner_slots(events, d, d, TZ) == []


def test_dinner_slots_one_per_day_in_order():
    slots = find_dinner_slots([], date(2026, 10, 14), date(2026, 10, 20), TZ, max_slots=3)
    assert len(slots) == 3
    assert [s.start.date() for s in slots] == sorted(s.start.date() for s in slots)
    assert all(s.start.time() == time(18, 30) for s in slots)


def test_ranking_respects_price_and_dietary_filters():
    req = DinnerRequest(date_from=WED, date_to=WED, max_price_level=2, dietary=["vegan"])
    top, _ = rank_restaurants(sample_restaurants(), req, SAMPLE_PREFS)
    assert top and all(r.price_level <= 2 and "vegan" in r.dietary for r, _ in top)


def test_ranking_prefers_requested_cuisine():
    req = DinnerRequest(date_from=WED, date_to=WED, cuisine="Japanese")
    top, warnings = rank_restaurants(sample_restaurants(), req, SAMPLE_PREFS)
    assert top[0][0].cuisine == "Japanese" and warnings == []


def test_ranking_warns_when_cuisine_unavailable():
    req = DinnerRequest(date_from=WED, date_to=WED, cuisine="Ethiopian")
    top, warnings = rank_restaurants(sample_restaurants(), req, SAMPLE_PREFS)
    assert len(top) == 3 and any("Ethiopian" in w for w in warnings)


def test_email_priority_vip_and_bulk():
    now = datetime(2026, 10, 14, 7, 0, tzinfo=TZ)
    ranked = rank_emails(sample_emails(now), SAMPLE_PREFS["vip_senders"], now)
    ids = [e.id for e, _, _ in ranked]
    assert ids[0] == "m1"          # VIP + deadline words
    assert "m5" not in ids         # bulk digest is filtered out
