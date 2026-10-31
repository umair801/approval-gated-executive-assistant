"""Pydantic models. LLM outputs are validated against these before use."""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

ActionKind = Literal["send_message", "book_table", "pay"]
WRITE_KINDS: frozenset[str] = frozenset({"send_message", "book_table", "pay"})
ActionStatus = Literal["pending", "executed", "rejected", "expired", "failed", "superseded"]


class CalendarEvent(BaseModel):
    id: str
    title: str
    start: datetime
    end: datetime
    location: str | None = None
    attendees: list[str] = Field(default_factory=list)
    all_day: bool = False


class EmailSummary(BaseModel):
    """Metadata only: sender, subject and Gmail's short snippet. Bodies are never fetched."""
    id: str
    sender: str
    subject: str
    snippet: str = ""
    received: datetime
    unread: bool = True


class Restaurant(BaseModel):
    id: str
    name: str
    cuisine: str
    price_level: int = Field(ge=1, le=4)
    rating: float = Field(ge=0, le=5)
    neighborhood: str = ""
    distance_km: float = 0.0
    dietary: list[str] = Field(default_factory=list)
    link: str | None = None  # maps or booking deep link where no API exists
    source: Literal["sample", "places"] = "sample"


class Slot(BaseModel):
    start: datetime
    end: datetime


class Conflict(BaseModel):
    kind: Literal["overlap", "back_to_back", "overload"]
    event_ids: list[str] = Field(default_factory=list)
    note: str


class PriorityItem(BaseModel):
    ref_type: Literal["event", "email"]
    ref_id: str
    reason: str
    rank: int = Field(ge=1)


class BriefingPlan(BaseModel):
    date: str
    headline: str
    summary: str
    priorities: list[PriorityItem] = Field(default_factory=list, max_length=5)
    conflicts: list[Conflict] = Field(default_factory=list)
    focus_blocks: list[Slot] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# ---- Small structured outputs the LLM produces ----

class IntentResult(BaseModel):
    intent: Literal["briefing", "dinner", "chat"]
    reason: str = ""


class ChatReply(BaseModel):
    text: str = Field(max_length=1500)


class DinnerRequest(BaseModel):
    friend_name: str | None = None
    date_from: date
    date_to: date
    party_size: int = Field(default=2, ge=1, le=12)
    cuisine: str | None = None
    max_price_level: int | None = Field(default=None, ge=1, le=4)
    dietary: list[str] = Field(default_factory=list)

    @field_validator("date_to")
    @classmethod
    def _order(cls, v: date, info):  # type: ignore[no-untyped-def]
        start = info.data.get("date_from")
        if start and v < start:
            raise ValueError("date_to before date_from")
        return v


class RestaurantNote(BaseModel):
    restaurant_id: str
    note: str = Field(max_length=300)


class DinnerDraft(BaseModel):
    message: str = Field(min_length=1, max_length=600)
    notes: list[RestaurantNote] = Field(default_factory=list)


class DinnerOption(BaseModel):
    restaurant: Restaurant
    slot: Slot
    score: float
    note: str = ""


class DinnerPlan(BaseModel):
    request: DinnerRequest
    slots: list[Slot]
    options: list[DinnerOption]
    draft_message: str
    friend_handle: str | None = None
    warnings: list[str] = Field(default_factory=list)


class ProposedAction(BaseModel):
    action_id: str
    user_id: str
    kind: ActionKind
    payload: dict
    payload_hash: str
    summary: str
    status: ActionStatus = "pending"
    flow_id: str | None = None
    idempotency_key: str
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    result: dict | None = None
