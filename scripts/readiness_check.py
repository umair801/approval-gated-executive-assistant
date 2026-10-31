"""Pre-launch checks. Prints PASS / FAIL / SKIP per item and exits non-zero if anything failed.

    python scripts/readiness_check.py                                     # local configuration
    python scripts/readiness_check.py --url https://your.host --token X   # also checks a deployed instance
    python scripts/readiness_check.py --ping-model                        # makes one tiny model call
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
results: list[tuple[str, str, str]] = []


def rec(status: str, name: str, detail: str = "") -> None:
    results.append((status, name, detail))
    print(f"[{status:4}] {name}" + (f"  {detail}" if detail else ""))


def http(url: str, method: str = "GET", headers: dict | None = None, timeout: float = 20.0):
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def check_config() -> None:
    from app.config import load_settings
    s = load_settings()
    errs = s.validate()
    rec("FAIL" if errs else "PASS", "configuration valid", "; ".join(errs))
    rec("PASS" if s.mode in ("private", "demo") else "SKIP", f"access mode is {s.mode}",
        "" if s.mode != "local" else "local mode is open; fine for development only")
    if s.token_encryption_key:
        try:
            from app.crypto import TokenVault
            TokenVault(s.token_encryption_key)
            rec("PASS", "TOKEN_ENCRYPTION_KEY is a valid Fernet key")
        except Exception as e:  # noqa: BLE001
            rec("FAIL", "TOKEN_ENCRYPTION_KEY is a valid Fernet key", str(e))
    else:
        rec("SKIP", "TOKEN_ENCRYPTION_KEY", "not set (required in production)")
    if s.access_token:
        rec("PASS" if len(s.access_token) >= 24 else "FAIL", "ACCESS_TOKEN length >= 24", f"{len(s.access_token)} chars")
    if s.database_url:
        try:
            import psycopg
            with psycopg.connect(s.database_url, connect_timeout=10) as c:
                c.execute("select 1")
                from app.context import SCHEMA_PATH
                from app.store import normalize_prefix, prefix_sql, TABLE_NAMES
                pre = normalize_prefix(s.table_prefix)
                c.execute(prefix_sql(SCHEMA_PATH.read_text(), pre))
                tables = [r[0] for r in c.execute("select tablename from pg_tables where schemaname='public'").fetchall()]
                need = {pre + t for t in TABLE_NAMES}
                rec("PASS" if need <= set(tables) else "FAIL", "database reachable, schema applied", f"missing: {sorted(need - set(tables))}" if not need <= set(tables) else "")
                off = [r[0] for r in c.execute("select relname from pg_class c join pg_namespace n on n.oid=c.relnamespace "
                                              "where n.nspname='public' and c.relkind='r' and not c.relrowsecurity").fetchall()]
                off = [t for t in off if t in need]  # shared database: only judge this project's tables
                rec("PASS" if not off else "FAIL", "row level security enabled on all tables", f"off: {off}" if off else "")
        except Exception as e:  # noqa: BLE001
            rec("FAIL", "database reachable, schema applied", f"{type(e).__name__}: {str(e)[:120]}")
    else:
        rec("SKIP", "database", "DATABASE_URL not set (in-memory store)")
    if s.data_mode == "google":
        ok = bool(s.google_client_id and s.google_client_secret and s.google_redirect_uri.startswith(("https://", "http://localhost")))
        rec("PASS" if ok else "FAIL", "Google client configured", s.google_redirect_uri)
    rec("PASS" if s.anthropic_api_key else "SKIP", "Claude API key present", "" if s.anthropic_api_key else "mock model will be used")


def check_model() -> None:
    from app.config import load_settings
    from app.llm import ClaudeLLM
    from app.models import ChatReply
    s = load_settings()
    if not s.anthropic_api_key:
        rec("SKIP", "model ping", "no ANTHROPIC_API_KEY"); return
    try:
        out = ClaudeLLM(s.anthropic_api_key, s.claude_model, 30, 5).structured("chat", ChatReply, "Say hello in five words.", {})
        rec("PASS", f"model responds ({s.claude_model})", out.text[:60])
    except Exception as e:  # noqa: BLE001
        rec("FAIL", "model responds", f"{type(e).__name__}: {str(e)[:120]}")


def check_remote(url: str, token: str | None) -> None:
    url = url.rstrip("/")
    st, _, body = http(f"{url}/health")
    ok = st == 200 and json.loads(body or b"{}").get("status") == "ok"
    rec("PASS" if ok else "FAIL", "GET /health returns ok", f"{st} {body[:120].decode(errors='ignore')}")
    if not ok:
        return
    info = json.loads(body)
    rec("PASS" if url.startswith("https://") else "FAIL", "served over HTTPS")
    rec("PASS" if info.get("store") == "postgres" else "FAIL", "deployment uses PostgreSQL", info.get("store", ""))
    _, h, _ = http(f"{url}/")
    h = {k.lower(): v for k, v in h.items()}
    rec("PASS" if "content-security-policy" in h else "FAIL", "security headers present")
    if info.get("access_mode") == "private":
        st, _, _ = http(f"{url}/api/approvals")
        rec("PASS" if st == 401 else "FAIL", "API rejects unauthenticated requests", str(st))
        if token:
            st, _, _ = http(f"{url}/api/approvals", headers={"Authorization": f"Bearer {token}"})
            rec("PASS" if st == 200 else "FAIL", "API accepts the access token", str(st))
    elif info.get("access_mode") == "demo":
        rec("PASS" if info.get("data_mode") == "sample" else "FAIL", "public demo uses sample data only")
    st, _, _ = http(f"{url}/static/htmx.min.js")
    rec("PASS" if st == 200 else "FAIL", "static assets served", str(st))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url"); ap.add_argument("--token"); ap.add_argument("--ping-model", action="store_true")
    a = ap.parse_args()
    check_config()
    if a.ping_model:
        check_model()
    if a.url:
        check_remote(a.url, a.token)
    failed = [r for r in results if r[0] == "FAIL"]
    print(f"\n{len(results) - len(failed)} ok or skipped, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
