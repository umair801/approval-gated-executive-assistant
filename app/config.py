"""Settings loaded from environment variables only. No secrets in code."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

VERSION = "1.0.0"


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _int(v: str | None, default: int) -> int:
    try:
        return int(v) if v not in (None, "") else default
    except ValueError:
        return default


def _float(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


@dataclass(frozen=True)
class Settings:
    app_env: str = "development"
    data_mode: str = "sample"  # sample | google
    access_token: str | None = None
    allow_public_demo: bool = False
    cron_token: str | None = None
    default_user: str = "owner"

    anthropic_api_key: str | None = None
    claude_model: str = "claude-sonnet-5-5"
    price_in_per_mtok: float | None = None
    price_out_per_mtok: float | None = None
    daily_llm_call_cap: int = 300
    llm_timeout_s: float = 45.0

    database_url: str | None = None
    table_prefix: str = ""
    token_encryption_key: str | None = None

    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"
    places_api_key: str | None = None

    timezone: str = "Asia/Shanghai"
    briefing_enabled: bool = False
    briefing_time: str = "07:30"
    approval_ttl_hours: int = 24
    rate_limit_per_min: int = 30
    http_timeout_s: float = 15.0

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def mode(self) -> str:
        """private: login + real data. demo: sandbox per visitor. local: open dev."""
        if self.access_token:
            return "private"
        if self.allow_public_demo:
            return "demo"
        return "local"

    @property
    def effective_data_mode(self) -> str:
        # The public demo never touches real accounts.
        return "sample" if self.mode == "demo" else self.data_mode

    @property
    def llm_mode(self) -> str:
        return "claude" if self.anthropic_api_key else "mock"

    def validate(self) -> list[str]:
        """Return a list of configuration errors. Production refuses to start with any."""
        errs: list[str] = []
        if self.data_mode not in {"sample", "google"}:
            errs.append("DATA_MODE must be 'sample' or 'google'.")
        if self.is_production and self.mode == "local":
            errs.append("Production needs ACCESS_TOKEN (private) or ALLOW_PUBLIC_DEMO=true (sample data only).")
        if self.table_prefix:
            try:
                from .store import normalize_prefix
                normalize_prefix(self.table_prefix)
            except ValueError as exc:
                errs.append(str(exc))
        if self.is_production and not self.token_encryption_key:
            errs.append("TOKEN_ENCRYPTION_KEY is required in production.")
        if self.mode != "demo" and self.data_mode == "google":
            if not (self.google_client_id and self.google_client_secret):
                errs.append("DATA_MODE=google needs GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.")
            if not self.token_encryption_key:
                errs.append("DATA_MODE=google needs TOKEN_ENCRYPTION_KEY to store tokens.")
        return errs


def _load_dotenv(path: str = ".env") -> None:
    """Read KEY=VALUE lines from a local .env file. Real environment variables always win."""
    if "PYTEST_CURRENT_TEST" in os.environ:  # tests must never pick up a developer's real keys
        return
    try:
        lines = open(path, encoding="utf-8-sig").read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if val[:1] in "\"'" and val[-1:] == val[:1] and len(val) > 1:
            val = val[1:-1]
        else:
            val = val.split(" #")[0].strip()
        os.environ.setdefault(key.strip(), val)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    if env is None:
        _load_dotenv()
    e = os.environ if env is None else env
    g = lambda k: (e.get(k) or None)  # noqa: E731
    return Settings(
        app_env=(g("APP_ENV") or "development").lower(),
        data_mode=(g("DATA_MODE") or "sample").lower(),
        access_token=g("ACCESS_TOKEN"),
        allow_public_demo=_bool(g("ALLOW_PUBLIC_DEMO")),
        cron_token=g("CRON_TOKEN"),
        default_user=g("DEFAULT_USER") or "owner",
        anthropic_api_key=g("ANTHROPIC_API_KEY"),
        claude_model=g("CLAUDE_MODEL") or "claude-sonnet-5-5",
        price_in_per_mtok=_float(g("PRICE_IN_PER_MTOK")),
        price_out_per_mtok=_float(g("PRICE_OUT_PER_MTOK")),
        daily_llm_call_cap=_int(g("DAILY_LLM_CALL_CAP"), 300),
        database_url=g("DATABASE_URL"),
        table_prefix=g("TABLE_PREFIX") or "",
        token_encryption_key=g("TOKEN_ENCRYPTION_KEY"),
        google_client_id=g("GOOGLE_CLIENT_ID"),
        google_client_secret=g("GOOGLE_CLIENT_SECRET"),
        google_redirect_uri=g("GOOGLE_REDIRECT_URI") or "http://localhost:8000/auth/google/callback",
        places_api_key=g("PLACES_API_KEY"),
        timezone=g("TIMEZONE") or "Asia/Shanghai",
        briefing_enabled=_bool(g("BRIEFING_ENABLED")),
        briefing_time=g("BRIEFING_TIME") or "07:30",
        approval_ttl_hours=_int(g("APPROVAL_TTL_HOURS"), 24),
        rate_limit_per_min=_int(g("RATE_LIMIT_PER_MIN"), 30),
    )
