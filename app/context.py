"""Wires settings, store, LLM, gate, tools and data sources into one object."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import httpx

from .config import Settings
from .crypto import TokenVault
from .gate import ApprovalGate
from .llm import LLM, ClaudeLLM, MockLLM
from .store import PostgresStore, Store, build_store, utcnow
from .tools import sample_data
from .tools.google import (GoogleCalendarAdapter, GoogleClient, GoogleEmailAdapter, GooglePlaces)
from .tools.registry import ToolRegistry
from .tools.sources import SampleCalendar, SampleEmail, SamplePlaces, Sources

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


class ConfigError(Exception):
    pass


@dataclass
class AppContext:
    settings: Settings
    store: Store
    vault: TokenVault
    llm: LLM
    gate: ApprovalGate
    registry: ToolRegistry
    sources: Sources
    google: GoogleClient | None
    clock: Callable[[], datetime]
    tz: ZoneInfo
    cache: dict[str, Any] = field(default_factory=dict)


def build_context(settings: Settings, *, store: Store | None = None, llm: LLM | None = None,
                  clock: Callable[[], datetime] = utcnow, sources: Sources | None = None,
                  http: httpx.Client | None = None) -> AppContext:
    errs = settings.validate()
    if errs:
        raise ConfigError("; ".join(errs))
    tz = ZoneInfo(settings.timezone)
    store = store or build_store(settings.database_url, settings.table_prefix)
    if isinstance(store, PostgresStore) and SCHEMA_PATH.exists():
        store.ensure_schema(SCHEMA_PATH.read_text())
    vault = TokenVault(settings.token_encryption_key)
    if llm is None:
        llm = (ClaudeLLM(settings.anthropic_api_key, settings.claude_model, settings.llm_timeout_s,
                         settings.daily_llm_call_cap) if settings.anthropic_api_key else MockLLM())
    gate = ApprovalGate(store, settings.approval_ttl_hours, clock)

    google = None
    if sources is None:
        if settings.effective_data_mode == "google":
            google = GoogleClient(settings, store, vault, http)
            places = GooglePlaces(settings.places_api_key, http) if settings.places_api_key else SamplePlaces()
            sources = Sources(GoogleCalendarAdapter(google), GoogleEmailAdapter(google), places)
        else:
            sources = Sources(SampleCalendar(clock, tz), SampleEmail(clock), SamplePlaces())

    reg = ToolRegistry(store)
    reg.read("calendar.list_events", lambda uid, start, end: sources.calendar.events(uid, start, end))
    reg.read("email.list_recent", lambda uid, since: sources.email.recent(uid, since))
    reg.read("places.search", lambda uid, cuisine=None, area=None: sources.places.search(uid, cuisine, area))
    reg.read("contacts.list", lambda uid: store.list_contacts(uid))
    reg.read("prefs.get", lambda uid: store.get_prefs(uid))
    for w in ("send_message", "book_table", "pay"):
        reg.write_placeholder(w)

    return AppContext(settings, store, vault, llm, gate, reg, sources, google, clock, tz)


def ensure_user(ctx: AppContext, user_id: str) -> None:
    """Register the user. In sample mode, seed fictional preferences and contacts once."""
    ctx.store.touch_user(user_id)
    if ctx.settings.effective_data_mode == "sample" and not ctx.store.get_prefs(user_id):
        ctx.store.set_prefs(user_id, dict(sample_data.SAMPLE_PREFS))
        for c in sample_data.SAMPLE_CONTACTS:
            ctx.store.upsert_contact(user_id, c["name"], c["handle"], c["notes"])
