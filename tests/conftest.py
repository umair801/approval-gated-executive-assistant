from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from app.gate import ApprovalGate
from app.store import MemoryStore, PostgresStore, Store


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 10, 14, 6, 0, tzinfo=timezone.utc)  # a Wednesday

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now = self.now + timedelta(**kw)


def _store_params():
    params = ["memory"]
    if os.environ.get("TEST_DATABASE_URL"):
        params.append("postgres")
    return params


@pytest.fixture(params=_store_params())
def store(request) -> Store:
    if request.param == "memory":
        yield MemoryStore()
        return
    s = PostgresStore(os.environ["TEST_DATABASE_URL"], prefix=os.environ.get("TEST_TABLE_PREFIX", ""))
    schema = open(os.path.join(os.path.dirname(__file__), "..", "schema.sql")).read()
    s.ensure_schema(schema)
    for t in ["audit_log", "outbox", "approvals", "actions", "messages", "plans", "oauth_tokens",
              "contacts", "preferences", "app_users"]:
        s._exec(f"truncate {t} cascade")
    yield s
    s.close()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def gate(store, clock) -> ApprovalGate:
    return ApprovalGate(store, ttl_hours=24, clock=clock)


MSG = {"to": "Sam", "text": "Dinner Friday at 8?"}
BOOK = {"restaurant_id": "r1", "restaurant_name": "Test Trattoria", "start": "2026-10-16T20:00:00+08:00",
        "end": "2026-10-16T21:30:00+08:00", "party_size": 2}


# ---- app-level fixtures -------------------------------------------------------------------
from app.config import Settings  # noqa: E402
from app.context import build_context  # noqa: E402


@pytest.fixture
def morning_clock() -> FakeClock:
    """Wednesday 2026-10-14 08:00 in Asia/Shanghai."""
    return FakeClock(datetime(2026, 10, 14, 0, 0, tzinfo=timezone.utc))


@pytest.fixture
def ctx(store, morning_clock):
    return build_context(Settings(), store=store, clock=morning_clock)
