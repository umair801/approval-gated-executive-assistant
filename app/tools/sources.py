"""Data sources behind small interfaces: sample data or Google (read-only)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from ..models import CalendarEvent, EmailSummary, Restaurant
from . import sample_data


class CalendarSource(Protocol):
    def events(self, user_id: str, start: datetime, end: datetime) -> list[CalendarEvent]: ...


class EmailSource(Protocol):
    def recent(self, user_id: str, since: datetime) -> list[EmailSummary]: ...


class PlacesSource(Protocol):
    def search(self, user_id: str, cuisine: str | None, area: str | None) -> list[Restaurant]: ...


class SampleCalendar:
    def __init__(self, clock: Callable[[], datetime], tz: ZoneInfo) -> None:
        self.clock, self.tz = clock, tz

    def events(self, user_id: str, start: datetime, end: datetime) -> list[CalendarEvent]:
        today = self.clock().astimezone(self.tz).date()
        return [e for e in sample_data.sample_events(today, self.tz) if e.start < end and e.end > start]


class SampleEmail:
    def __init__(self, clock: Callable[[], datetime]) -> None:
        self.clock = clock

    def recent(self, user_id: str, since: datetime) -> list[EmailSummary]:
        return [m for m in sample_data.sample_emails(self.clock()) if m.received >= since]


class SamplePlaces:
    def search(self, user_id: str, cuisine: str | None, area: str | None) -> list[Restaurant]:
        return sample_data.sample_restaurants()


class Sources:
    def __init__(self, calendar: CalendarSource, email: EmailSource, places: PlacesSource) -> None:
        self.calendar, self.email, self.places = calendar, email, places
