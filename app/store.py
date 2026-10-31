"""Persistence layer.

Two implementations behind one interface:
  MemoryStore    default for local runs, demos and most tests
  PostgresStore  Supabase / PostgreSQL, used when DATABASE_URL is set

Both guarantee the same safety properties the approval gate relies on:
  * insert_action dedupes live proposals by idempotency key
  * record_approval is atomic and unique per action (idempotent approvals)
  * transition_action is compare-and-set on status
  * the audit log is append-only and hash-chained per user
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import uuid
from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from .models import ProposedAction

GENESIS = "0" * 64
SYSTEM_USER = "_system"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def payload_hash(payload: dict) -> str:
    return sha256(canonical(payload))


def idempotency_key(user_id: str, kind: str, payload: dict, flow_id: str | None) -> str:
    return sha256(canonical({"u": user_id, "k": kind, "p": payload, "f": flow_id}))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def audit_hash(prev_hash: str, user_id: str, ts: str, event: str, action_id: str | None,
               actor: str, details: dict) -> str:
    body = canonical({"u": user_id, "ts": ts, "e": event, "a": action_id, "actor": actor, "d": details})
    return sha256(prev_hash + body)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class Store(ABC):
    kind = "abstract"

    # users
    @abstractmethod
    def touch_user(self, user_id: str) -> None: ...
    @abstractmethod
    def purge_stale_users(self, prefix: str, older_than: datetime) -> list[str]: ...

    # preferences and contacts
    @abstractmethod
    def get_prefs(self, user_id: str) -> dict: ...
    @abstractmethod
    def set_prefs(self, user_id: str, data: dict) -> None: ...
    @abstractmethod
    def list_contacts(self, user_id: str) -> list[dict]: ...
    @abstractmethod
    def upsert_contact(self, user_id: str, name: str, handle: str | None, notes: str | None = None) -> None: ...

    # encrypted tokens
    @abstractmethod
    def save_token(self, user_id: str, provider: str, ciphertext: str, scopes: str | None) -> None: ...
    @abstractmethod
    def get_token(self, user_id: str, provider: str) -> str | None: ...

    # plans and chat
    @abstractmethod
    def save_plan(self, user_id: str, kind: str, data: dict) -> str: ...
    @abstractmethod
    def get_plan(self, user_id: str, plan_id: str) -> dict | None: ...
    @abstractmethod
    def latest_plan(self, user_id: str, kind: str) -> dict | None: ...
    @abstractmethod
    def add_message(self, user_id: str, role: str, text: str, meta: dict | None = None) -> None: ...
    @abstractmethod
    def list_messages(self, user_id: str, limit: int = 50) -> list[dict]: ...

    # actions, approvals, outbox
    @abstractmethod
    def insert_action(self, action: ProposedAction) -> tuple[ProposedAction, bool]: ...
    @abstractmethod
    def get_action(self, user_id: str, action_id: str) -> ProposedAction | None: ...
    @abstractmethod
    def list_actions(self, user_id: str, status: str | None = None, flow_id: str | None = None,
                     limit: int = 100) -> list[ProposedAction]: ...
    @abstractmethod
    def transition_action(self, user_id: str, action_id: str, from_status: str, to_status: str,
                          decided_by: str | None = None, result: dict | None = None) -> bool: ...
    @abstractmethod
    def update_pending_payload(self, user_id: str, action_id: str, payload: dict, new_hash: str,
                               summary: str, idem_key: str) -> bool: ...
    @abstractmethod
    def record_approval(self, user_id: str, action_id: str, p_hash: str, approved_by: str) -> tuple[dict, bool]: ...
    @abstractmethod
    def get_approval(self, action_id: str) -> dict | None: ...
    @abstractmethod
    def record_outbox(self, user_id: str, action_id: str, kind: str, payload: dict) -> dict: ...
    @abstractmethod
    def list_outbox(self, user_id: str) -> list[dict]: ...

    # audit
    @abstractmethod
    def append_audit(self, user_id: str, event: str, actor: str, action_id: str | None = None,
                     details: dict | None = None) -> dict: ...
    @abstractmethod
    def list_audit(self, user_id: str, limit: int = 200) -> list[dict]: ...
    @abstractmethod
    def verify_audit(self, user_id: str) -> tuple[bool, int | None]: ...

    # privacy
    @abstractmethod
    def export_user_data(self, user_id: str) -> dict: ...
    @abstractmethod
    def delete_user_data(self, user_id: str) -> dict: ...

    def ping(self) -> bool:
        return True


# ----------------------------------------------------------------------------------------
class MemoryStore(Store):
    kind = "memory"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._users: dict[str, dict] = {}
        self._prefs: dict[str, dict] = {}
        self._contacts: dict[str, dict[str, dict]] = {}
        self._tokens: dict[tuple[str, str], dict] = {}
        self._plans: dict[str, dict] = {}
        self._messages: list[dict] = []
        self._actions: dict[str, ProposedAction] = {}
        self._approvals: dict[str, dict] = {}
        self._outbox: dict[str, dict] = {}
        self._audit: list[dict] = []
        self._seq = 0

    # users
    def touch_user(self, user_id: str) -> None:
        with self._lock:
            now = utcnow()
            u = self._users.setdefault(user_id, {"created_at": now})
            u["last_seen"] = now

    def purge_stale_users(self, prefix: str, older_than: datetime) -> list[str]:
        with self._lock:
            stale = [u for u, v in self._users.items() if u.startswith(prefix) and v["last_seen"] < older_than]
        for u in stale:
            self.delete_user_data(u)
        return stale

    # prefs / contacts
    def get_prefs(self, user_id: str) -> dict:
        with self._lock:
            return copy.deepcopy(self._prefs.get(user_id, {}))

    def set_prefs(self, user_id: str, data: dict) -> None:
        with self._lock:
            self._prefs[user_id] = copy.deepcopy(data)

    def list_contacts(self, user_id: str) -> list[dict]:
        with self._lock:
            return [dict(c) for c in self._contacts.get(user_id, {}).values()]

    def upsert_contact(self, user_id: str, name: str, handle: str | None, notes: str | None = None) -> None:
        with self._lock:
            self._contacts.setdefault(user_id, {})[name] = {"name": name, "handle": handle, "notes": notes}

    # tokens
    def save_token(self, user_id: str, provider: str, ciphertext: str, scopes: str | None) -> None:
        with self._lock:
            self._tokens[(user_id, provider)] = {"ciphertext": ciphertext, "scopes": scopes}

    def get_token(self, user_id: str, provider: str) -> str | None:
        with self._lock:
            t = self._tokens.get((user_id, provider))
            return t["ciphertext"] if t else None

    # plans / chat
    def save_plan(self, user_id: str, kind: str, data: dict) -> str:
        pid = new_id("plan")
        with self._lock:
            self._plans[pid] = {"plan_id": pid, "user_id": user_id, "kind": kind,
                                "data": copy.deepcopy(data), "created_at": utcnow()}
        return pid

    def get_plan(self, user_id: str, plan_id: str) -> dict | None:
        with self._lock:
            p = self._plans.get(plan_id)
            return copy.deepcopy(p) if p and p["user_id"] == user_id else None

    def latest_plan(self, user_id: str, kind: str) -> dict | None:
        with self._lock:
            ps = [p for p in self._plans.values() if p["user_id"] == user_id and p["kind"] == kind]
            # Insertion order, not the clock: two saves can share a timestamp (coarse clocks, e.g. Windows).
            return copy.deepcopy(ps[-1]) if ps else None

    def add_message(self, user_id: str, role: str, text: str, meta: dict | None = None) -> None:
        with self._lock:
            self._messages.append({"user_id": user_id, "role": role, "text": text,
                                   "meta": meta or {}, "created_at": utcnow()})

    def list_messages(self, user_id: str, limit: int = 50) -> list[dict]:
        with self._lock:
            return [dict(m) for m in self._messages if m["user_id"] == user_id][-limit:]

    # actions
    def insert_action(self, action: ProposedAction) -> tuple[ProposedAction, bool]:
        with self._lock:
            for a in self._actions.values():
                if (a.user_id == action.user_id and a.idempotency_key == action.idempotency_key
                        and a.status in ("pending", "executed")):
                    return a.model_copy(deep=True), False
            self._actions[action.action_id] = action.model_copy(deep=True)
            return action.model_copy(deep=True), True

    def get_action(self, user_id: str, action_id: str) -> ProposedAction | None:
        with self._lock:
            a = self._actions.get(action_id)
            return a.model_copy(deep=True) if a and a.user_id == user_id else None

    def list_actions(self, user_id: str, status: str | None = None, flow_id: str | None = None,
                     limit: int = 100) -> list[ProposedAction]:
        with self._lock:
            xs = [a for a in self._actions.values() if a.user_id == user_id
                  and (status is None or a.status == status) and (flow_id is None or a.flow_id == flow_id)]
            xs.reverse()  # newest inserted first, so timestamp ties still come out newest first
            xs.sort(key=lambda a: a.created_at, reverse=True)  # stable sort keeps that order on ties
            return [a.model_copy(deep=True) for a in xs[:limit]]

    def transition_action(self, user_id: str, action_id: str, from_status: str, to_status: str,
                          decided_by: str | None = None, result: dict | None = None) -> bool:
        with self._lock:
            a = self._actions.get(action_id)
            if not a or a.user_id != user_id or a.status != from_status:
                return False
            a.status = to_status  # type: ignore[assignment]
            a.decided_at = utcnow()
            a.decided_by = decided_by
            if result is not None:
                a.result = copy.deepcopy(result)
            return True

    def update_pending_payload(self, user_id: str, action_id: str, payload: dict, new_hash: str,
                               summary: str, idem_key: str) -> bool:
        with self._lock:
            a = self._actions.get(action_id)
            if not a or a.user_id != user_id or a.status != "pending":
                return False
            a.payload = copy.deepcopy(payload)
            a.payload_hash = new_hash
            a.summary = summary
            a.idempotency_key = idem_key
            return True

    def record_approval(self, user_id: str, action_id: str, p_hash: str, approved_by: str) -> tuple[dict, bool]:
        with self._lock:
            if action_id in self._approvals:
                return dict(self._approvals[action_id]), False
            rec = {"approval_id": new_id("appr"), "action_id": action_id, "user_id": user_id,
                   "payload_hash": p_hash, "approved_by": approved_by, "approved_at": utcnow()}
            self._approvals[action_id] = rec
            return dict(rec), True

    def get_approval(self, action_id: str) -> dict | None:
        with self._lock:
            r = self._approvals.get(action_id)
            return dict(r) if r else None

    def record_outbox(self, user_id: str, action_id: str, kind: str, payload: dict) -> dict:
        with self._lock:
            if action_id in self._outbox:  # second line of defense: one side effect per action
                return dict(self._outbox[action_id])
            rec = {"outbox_id": new_id("out"), "action_id": action_id, "user_id": user_id, "kind": kind,
                   "payload": copy.deepcopy(payload), "executed_at": utcnow()}
            self._outbox[action_id] = rec
            return dict(rec)

    def list_outbox(self, user_id: str) -> list[dict]:
        with self._lock:
            return [dict(o) for o in self._outbox.values() if o["user_id"] == user_id]

    # audit
    def append_audit(self, user_id: str, event: str, actor: str, action_id: str | None = None,
                     details: dict | None = None) -> dict:
        with self._lock:
            chain = [e for e in self._audit if e["user_id"] == user_id]
            prev = chain[-1]["hash"] if chain else GENESIS
            ts = iso(utcnow())
            d = json.loads(canonical(details or {}))
            h = audit_hash(prev, user_id, ts, event, action_id, actor, d)
            self._seq += 1
            rec = {"seq": self._seq, "user_id": user_id, "ts": ts, "event": event, "action_id": action_id,
                   "actor": actor, "details": d, "prev_hash": prev, "hash": h}
            self._audit.append(rec)
            return dict(rec)

    def list_audit(self, user_id: str, limit: int = 200) -> list[dict]:
        with self._lock:
            xs = [dict(e) for e in self._audit if e["user_id"] == user_id]
            return xs[-limit:][::-1]

    def verify_audit(self, user_id: str) -> tuple[bool, int | None]:
        with self._lock:
            prev = GENESIS
            for e in (x for x in self._audit if x["user_id"] == user_id):
                if e["prev_hash"] != prev or e["hash"] != audit_hash(
                        prev, e["user_id"], e["ts"], e["event"], e["action_id"], e["actor"], e["details"]):
                    return False, e["seq"]
                prev = e["hash"]
            return True, None

    # privacy
    def export_user_data(self, user_id: str) -> dict:
        with self._lock:
            return {
                "preferences": self.get_prefs(user_id),
                "contacts": self.list_contacts(user_id),
                "connected_providers": sorted(p for (u, p) in self._tokens if u == user_id),
                "plans": [copy.deepcopy(p) for p in self._plans.values() if p["user_id"] == user_id],
                "messages": self.list_messages(user_id, 10_000),
                "actions": [a.model_dump(mode="json") for a in self.list_actions(user_id, limit=10_000)],
                "approvals": [dict(r) for r in self._approvals.values() if r["user_id"] == user_id],
                "outbox": self.list_outbox(user_id),
                "audit_log": list(reversed(self.list_audit(user_id, 100_000))),
            }

    def delete_user_data(self, user_id: str) -> dict:
        with self._lock:
            counts = {
                "preferences": int(user_id in self._prefs),
                "contacts": len(self._contacts.get(user_id, {})),
                "tokens": len([k for k in self._tokens if k[0] == user_id]),
                "plans": len([p for p in self._plans.values() if p["user_id"] == user_id]),
                "messages": len([m for m in self._messages if m["user_id"] == user_id]),
                "actions": len([a for a in self._actions.values() if a.user_id == user_id]),
                "audit_entries": len([e for e in self._audit if e["user_id"] == user_id]),
            }
            self._prefs.pop(user_id, None)
            self._contacts.pop(user_id, None)
            for k in [k for k in self._tokens if k[0] == user_id]:
                del self._tokens[k]
            for k in [k for k, p in self._plans.items() if p["user_id"] == user_id]:
                del self._plans[k]
            self._messages = [m for m in self._messages if m["user_id"] != user_id]
            gone = {k for k, a in self._actions.items() if a.user_id == user_id}
            for k in gone:
                del self._actions[k]
                self._approvals.pop(k, None)
                self._outbox.pop(k, None)
            self._audit = [e for e in self._audit if e["user_id"] != user_id]
            self._users.pop(user_id, None)
        # Tombstone on the system chain: hashed id only, no personal data.
        self.append_audit(SYSTEM_USER, "user_data_deleted", "system",
                          details={"user_hash": sha256(user_id)[:16], "counts": counts})
        return counts


# ----------------------------------------------------------------------------------------
# Shared database support: every table and index name can carry a prefix (for example "abc_"),
# so several projects can live in one Supabase project without name clashes.
TABLE_NAMES = ("app_users", "preferences", "contacts", "oauth_tokens", "plans", "messages", "actions",
               "approvals", "outbox", "audit_log")
INDEX_NAMES = ("plans_user_kind_idx", "messages_user_idx", "actions_user_status_idx", "actions_live_key_idx",
               "audit_user_idx")
_NAME_RE = re.compile(r"\b(" + "|".join(sorted(TABLE_NAMES + INDEX_NAMES, key=len, reverse=True)) + r")\b")
_PREFIX_RE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")


def normalize_prefix(raw: str | None) -> str:
    """'abc', 'abc_' or 'my-app' become 'abc_', 'abc_', 'my_app_'. Empty means no prefix."""
    if not raw or not raw.strip():
        return ""
    stem = raw.strip().lower().replace("-", "_").strip("_")
    if not _PREFIX_RE.match(stem) or len(stem) > 30:
        raise ValueError("TABLE_PREFIX must be lowercase letters, digits and underscores, start with a letter, "
                         "and be at most 30 characters (your subdomain, for example 'assistant').")
    return stem + "_"


def prefix_sql(sql: str, prefix: str) -> str:
    """Put the prefix in front of every known table and index name in a SQL string."""
    return _NAME_RE.sub(lambda m: prefix + m.group(1), sql) if prefix else sql


class _PrefixedConn:
    """Thin wrapper: rewrites table names, passes everything else straight through."""

    def __init__(self, conn: Any, prefix: str) -> None:
        self._conn, self._prefix = conn, prefix

    def execute(self, sql: str, params: Any = None, **kw: Any):
        return self._conn.execute(prefix_sql(sql, self._prefix), params, **kw)

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


class PostgresStore(Store):
    kind = "postgres"

    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 5, prefix: str = "") -> None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        self.prefix = normalize_prefix(prefix)
        self._pool = ConnectionPool(
            dsn, min_size=min_size, max_size=max_size, open=True,
            # prepare_threshold=None: safe behind Supabase's transaction pooler (no server-side prepared statements).
            # No session options: poolers can reject them. Timestamps are converted explicitly in code.
            kwargs={"row_factory": dict_row, "prepare_threshold": None, "connect_timeout": 10},
        )
        self._pool.wait(timeout=20)

    @contextmanager
    def _connection(self):
        with self._pool.connection() as conn:
            yield _PrefixedConn(conn, self.prefix)

    # helpers
    def _exec(self, sql: str, params: tuple | dict = (), fetch: str | None = None):
        with self._connection() as conn:
            cur = conn.execute(sql, params)
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            return cur.rowcount

    @staticmethod
    def _j(v: Any):
        from psycopg.types.json import Jsonb
        return Jsonb(v)

    def ping(self) -> bool:
        try:
            return self._exec("select 1 as ok", fetch="one")["ok"] == 1
        except Exception:
            return False

    def ensure_schema(self, sql_text: str) -> None:
        with self._connection() as conn:
            conn.execute(sql_text)

    # users
    def touch_user(self, user_id: str) -> None:
        self._exec("insert into app_users (user_id) values (%s) on conflict (user_id) "
                   "do update set last_seen = now()", (user_id,))

    def purge_stale_users(self, prefix: str, older_than: datetime) -> list[str]:
        rows = self._exec("select user_id from app_users where user_id like %s and last_seen < %s",
                          (prefix.replace("%", "") + "%", older_than), fetch="all")
        ids = [r["user_id"] for r in rows]
        for u in ids:
            self.delete_user_data(u)
        return ids

    # prefs / contacts
    def get_prefs(self, user_id: str) -> dict:
        r = self._exec("select data from preferences where user_id=%s", (user_id,), fetch="one")
        return r["data"] if r else {}

    def set_prefs(self, user_id: str, data: dict) -> None:
        self._exec("insert into preferences (user_id, data) values (%s,%s) on conflict (user_id) "
                   "do update set data=excluded.data, updated_at=now()", (user_id, self._j(data)))

    def list_contacts(self, user_id: str) -> list[dict]:
        return self._exec("select name, handle, notes from contacts where user_id=%s order by name",
                          (user_id,), fetch="all")

    def upsert_contact(self, user_id: str, name: str, handle: str | None, notes: str | None = None) -> None:
        self._exec("insert into contacts (user_id,name,handle,notes) values (%s,%s,%s,%s) "
                   "on conflict (user_id,name) do update set handle=excluded.handle, notes=excluded.notes",
                   (user_id, name, handle, notes))

    # tokens
    def save_token(self, user_id: str, provider: str, ciphertext: str, scopes: str | None) -> None:
        self._exec("insert into oauth_tokens (user_id,provider,ciphertext,scopes) values (%s,%s,%s,%s) "
                   "on conflict (user_id,provider) do update set ciphertext=excluded.ciphertext, "
                   "scopes=excluded.scopes, updated_at=now()", (user_id, provider, ciphertext, scopes))

    def get_token(self, user_id: str, provider: str) -> str | None:
        r = self._exec("select ciphertext from oauth_tokens where user_id=%s and provider=%s",
                       (user_id, provider), fetch="one")
        return r["ciphertext"] if r else None

    # plans / chat
    def save_plan(self, user_id: str, kind: str, data: dict) -> str:
        pid = new_id("plan")
        self._exec("insert into plans (plan_id,user_id,kind,data) values (%s,%s,%s,%s)",
                   (pid, user_id, kind, self._j(data)))
        return pid

    def get_plan(self, user_id: str, plan_id: str) -> dict | None:
        return self._exec("select * from plans where plan_id=%s and user_id=%s", (plan_id, user_id), fetch="one")

    def latest_plan(self, user_id: str, kind: str) -> dict | None:
        return self._exec("select * from plans where user_id=%s and kind=%s order by created_at desc limit 1",
                          (user_id, kind), fetch="one")

    def add_message(self, user_id: str, role: str, text: str, meta: dict | None = None) -> None:
        self._exec("insert into messages (user_id,role,text,meta) values (%s,%s,%s,%s)",
                   (user_id, role, text, self._j(meta or {})))

    def list_messages(self, user_id: str, limit: int = 50) -> list[dict]:
        rows = self._exec("select user_id, role, text, meta, created_at from messages where user_id=%s "
                          "order by id desc limit %s", (user_id, limit), fetch="all")
        return rows[::-1]

    # actions
    @staticmethod
    def _to_action(r: dict) -> ProposedAction:
        return ProposedAction(
            action_id=r["action_id"], user_id=r["user_id"], kind=r["kind"], payload=r["payload"],
            payload_hash=r["payload_hash"], summary=r["summary"], status=r["status"], flow_id=r["flow_id"],
            idempotency_key=r["idempotency_key"], created_at=r["created_at"], expires_at=r["expires_at"],
            decided_at=r["decided_at"], decided_by=r["decided_by"], result=r["result"])

    def insert_action(self, action: ProposedAction) -> tuple[ProposedAction, bool]:
        import psycopg.errors as pe
        try:
            self._exec(
                "insert into actions (action_id,user_id,kind,payload,payload_hash,summary,status,flow_id,"
                "idempotency_key,created_at,expires_at) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (action.action_id, action.user_id, action.kind, self._j(action.payload), action.payload_hash,
                 action.summary, action.status, action.flow_id, action.idempotency_key,
                 action.created_at, action.expires_at))
            return action, True
        except pe.UniqueViolation:
            r = self._exec("select * from actions where user_id=%s and idempotency_key=%s and "
                           "status in ('pending','executed')", (action.user_id, action.idempotency_key),
                           fetch="one")
            if not r:  # raced with a status change, treat as new attempt
                return self.insert_action(action.model_copy(update={"action_id": new_id("act")}))
            return self._to_action(r), False

    def get_action(self, user_id: str, action_id: str) -> ProposedAction | None:
        r = self._exec("select * from actions where action_id=%s and user_id=%s", (action_id, user_id), fetch="one")
        return self._to_action(r) if r else None

    def list_actions(self, user_id: str, status: str | None = None, flow_id: str | None = None,
                     limit: int = 100) -> list[ProposedAction]:
        rows = self._exec(
            "select * from actions where user_id=%s and (%s::text is null or status=%s) "
            "and (%s::text is null or flow_id=%s) order by created_at desc limit %s",
            (user_id, status, status, flow_id, flow_id, limit), fetch="all")
        return [self._to_action(r) for r in rows]

    def transition_action(self, user_id: str, action_id: str, from_status: str, to_status: str,
                          decided_by: str | None = None, result: dict | None = None) -> bool:
        n = self._exec(
            "update actions set status=%s, decided_at=now(), decided_by=%s, result=coalesce(%s, result) "
            "where action_id=%s and user_id=%s and status=%s",
            (to_status, decided_by, self._j(result) if result is not None else None,
             action_id, user_id, from_status))
        return n == 1

    def update_pending_payload(self, user_id: str, action_id: str, payload: dict, new_hash: str,
                               summary: str, idem_key: str) -> bool:
        n = self._exec("update actions set payload=%s, payload_hash=%s, summary=%s, idempotency_key=%s "
                       "where action_id=%s and user_id=%s and status='pending'",
                       (self._j(payload), new_hash, summary, idem_key, action_id, user_id))
        return n == 1

    def record_approval(self, user_id: str, action_id: str, p_hash: str, approved_by: str) -> tuple[dict, bool]:
        aid = new_id("appr")
        with self._connection() as conn:
            cur = conn.execute(
                "insert into approvals (approval_id,action_id,user_id,payload_hash,approved_by) "
                "values (%s,%s,%s,%s,%s) on conflict (action_id) do nothing returning *",
                (aid, action_id, user_id, p_hash, approved_by))
            row = cur.fetchone()
            if row:
                return row, True
            return conn.execute("select * from approvals where action_id=%s", (action_id,)).fetchone(), False

    def get_approval(self, action_id: str) -> dict | None:
        return self._exec("select * from approvals where action_id=%s", (action_id,), fetch="one")

    def record_outbox(self, user_id: str, action_id: str, kind: str, payload: dict) -> dict:
        with self._connection() as conn:
            row = conn.execute(
                "insert into outbox (outbox_id,action_id,user_id,kind,payload) values (%s,%s,%s,%s,%s) "
                "on conflict (action_id) do nothing returning *",
                (new_id("out"), action_id, user_id, kind, self._j(payload))).fetchone()
            return row or conn.execute("select * from outbox where action_id=%s", (action_id,)).fetchone()

    def list_outbox(self, user_id: str) -> list[dict]:
        return self._exec("select * from outbox where user_id=%s order by executed_at", (user_id,), fetch="all")

    # audit
    def append_audit(self, user_id: str, event: str, actor: str, action_id: str | None = None,
                     details: dict | None = None) -> dict:
        d = json.loads(canonical(details or {}))
        with self._connection() as conn:
            conn.execute("select pg_advisory_xact_lock(hashtext(%s))", (user_id,))
            last = conn.execute("select hash from audit_log where user_id=%s order by seq desc limit 1",
                                (user_id,)).fetchone()
            prev = last["hash"] if last else GENESIS
            now = utcnow()
            ts = iso(now)
            h = audit_hash(prev, user_id, ts, event, action_id, actor, d)
            row = conn.execute(
                "insert into audit_log (user_id,ts,event,action_id,actor,details,prev_hash,hash) "
                "values (%s,%s,%s,%s,%s,%s,%s,%s) returning *",
                (user_id, now, event, action_id, actor, self._j(d), prev, h)).fetchone()
            row["ts"] = iso(row["ts"])
            return row

    def list_audit(self, user_id: str, limit: int = 200) -> list[dict]:
        rows = self._exec("select * from audit_log where user_id=%s order by seq desc limit %s",
                          (user_id, limit), fetch="all")
        for r in rows:
            r["ts"] = iso(r["ts"])
        return rows

    def verify_audit(self, user_id: str) -> tuple[bool, int | None]:
        rows = self._exec("select * from audit_log where user_id=%s order by seq", (user_id,), fetch="all")
        prev = GENESIS
        for e in rows:
            if e["prev_hash"] != prev or e["hash"] != audit_hash(
                    prev, e["user_id"], iso(e["ts"]), e["event"], e["action_id"], e["actor"], e["details"]):
                return False, e["seq"]
            prev = e["hash"]
        return True, None

    # privacy
    def export_user_data(self, user_id: str) -> dict:
        q = lambda sql: self._exec(sql, (user_id,), fetch="all")  # noqa: E731
        return {
            "preferences": self.get_prefs(user_id),
            "contacts": self.list_contacts(user_id),
            "connected_providers": [r["provider"] for r in q("select provider from oauth_tokens where user_id=%s")],
            "plans": q("select * from plans where user_id=%s"),
            "messages": q("select role,text,meta,created_at from messages where user_id=%s order by id"),
            "actions": [a.model_dump(mode="json") for a in self.list_actions(user_id, limit=10_000)],
            "approvals": q("select * from approvals where user_id=%s"),
            "outbox": q("select * from outbox where user_id=%s"),
            "audit_log": q("select * from audit_log where user_id=%s order by seq"),
        }

    def delete_user_data(self, user_id: str) -> dict:
        counts = {}
        for label, sql in [
            ("audit_entries", "delete from audit_log where user_id=%s"),
            ("outbox", "delete from outbox where user_id=%s"),
            ("approvals", "delete from approvals where user_id=%s"),
            ("actions", "delete from actions where user_id=%s"),
            ("messages", "delete from messages where user_id=%s"),
            ("plans", "delete from plans where user_id=%s"),
            ("tokens", "delete from oauth_tokens where user_id=%s"),
            ("contacts", "delete from contacts where user_id=%s"),
            ("preferences", "delete from preferences where user_id=%s"),
            ("users", "delete from app_users where user_id=%s"),
        ]:
            counts[label] = self._exec(sql, (user_id,))
        self.append_audit(SYSTEM_USER, "user_data_deleted", "system",
                          details={"user_hash": sha256(user_id)[:16], "counts": counts})
        return counts

    def close(self) -> None:
        self._pool.close()


def build_store(database_url: str | None, prefix: str = "") -> Store:
    if database_url:
        return PostgresStore(database_url, prefix=prefix)
    return MemoryStore()
