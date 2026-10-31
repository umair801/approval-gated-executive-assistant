"""FastAPI app: JSON API, HTMX pages, auth modes, rate limiting, health."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Form, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .actions import InvalidAction
from .config import VERSION, Settings, load_settings
from .context import AppContext, build_context, ensure_user
from .executors import ApprovalRequired, PayloadTampered
from .flows import ChoiceError, choose_restaurant, run_briefing, run_briefing_if_due, run_chat
from .gate import ActionConflict, ActionExpired, ActionNotFound
from .llm import BudgetExceeded
from .logging_setup import get_logger, setup_logging
from .models import DinnerPlan
from .net import UpstreamError
from .scheduler import DEMO_PREFIX, scheduler_loop
from .store import SYSTEM_USER
from .tools.google import NotConnected
from .tools.registry import WriteToolBlocked

log = get_logger("api")
BASE = Path(__file__).resolve().parent
SESSION_COOKIE, UID_COOKIE = "ea_session", "ea_uid"
OPEN_PATHS = {"/health", "/login", "/robots.txt"}
UID_RE = re.compile(r"^demo-[0-9a-f]{16}$")


def _session_value(token: str) -> str:
    return hmac.new(token.encode(), b"ea-session-v1", hashlib.sha256).hexdigest()


class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.n, self.hits = per_minute, defaultdict(deque)

    def allow(self, key: str, limit: int | None = None) -> bool:
        now, q, cap = time.monotonic(), self.hits[key], limit or self.n
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= cap:
            return False
        q.append(now)
        return True


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=500)


class ApproveIn(BaseModel):
    edited_text: str | None = Field(default=None, max_length=600)


class RejectIn(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


class ChooseIn(BaseModel):
    restaurant_id: str = Field(min_length=1, max_length=200)


class ContactIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    handle: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=200)


class PrefsIn(BaseModel):
    home_area: str | None = Field(default=None, max_length=60)
    favorite_cuisines: list[str] = Field(default_factory=list, max_length=8)
    max_price_level: int | None = Field(default=None, ge=1, le=4)
    vip_senders: list[str] = Field(default_factory=list, max_length=10)
    dietary: list[str] = Field(default_factory=list, max_length=6)


def create_app(settings: Settings | None = None, ctx: AppContext | None = None) -> FastAPI:
    settings = settings or (ctx.settings if ctx else load_settings())
    setup_logging()
    ctx = ctx or build_context(settings)
    limiter = RateLimiter(settings.rate_limit_per_min)
    login_limiter = RateLimiter(10)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if settings.briefing_enabled or settings.mode == "demo":
            task = asyncio.create_task(scheduler_loop(ctx))
        log.info("started", extra={"version": VERSION, "mode": settings.mode, "data_mode": settings.effective_data_mode,
                                   "llm": ctx.llm.mode, "store": ctx.store.kind})
        yield
        if task:
            task.cancel()
        close = getattr(ctx.store, "close", None)
        if close:
            close()

    app = FastAPI(title="Executive Assistant Agent", version=VERSION, lifespan=lifespan,
                  description="Plans your day, drafts messages and shortlists restaurants. "
                              "Anything that sends, books or pays waits for your approval.")
    app.state.ctx = ctx
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    templates = Jinja2Templates(directory=BASE / "templates")

    def fmt_dt(v) -> str:
        if not v:
            return ""
        if isinstance(v, str):
            v = datetime.fromisoformat(v)
        return v.astimezone(ctx.tz).strftime("%a %d %b, %H:%M")

    def fmt_time(v) -> str:
        if isinstance(v, str):
            v = datetime.fromisoformat(v)
        return v.astimezone(ctx.tz).strftime("%H:%M")

    templates.env.filters["dt"] = fmt_dt
    templates.env.filters["hm"] = fmt_time

    # ------------------------------------------------------------------ middleware
    def secure(response, path: str):
        """Security headers go on EVERY response, including 401, 403, 429 and 500."""
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if not path.startswith(("/docs", "/redoc", "/openapi")):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
                "img-src 'self' data:; frame-ancestors 'none'; form-action 'self'")
        return response

    @app.middleware("http")
    async def gatekeeper(request: Request, call_next):
        path, method = request.url.path, request.method
        new_uid: str | None = None
        client = request.client.host if request.client else "unknown"

        def deny(status: int, detail: str):
            wants_html = "text/html" in request.headers.get("accept", "") and not path.startswith("/api")
            if status == 401 and wants_html:
                return secure(RedirectResponse("/login", status_code=303), path)
            return secure(JSONResponse({"error": "denied", "detail": detail}, status_code=status), path)

        # Cross-site POSTs are refused (cookies are SameSite=Strict as well).
        if method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host", ""):
                return deny(403, "Cross-origin request refused")
            if not limiter.allow(f"{client}:{method}"):
                return secure(JSONResponse({"error": "rate_limited", "detail": "Too many requests. Slow down."},
                                           status_code=429, headers={"Retry-After": "30"}), path)

        cron = path == "/api/jobs/briefing"
        if cron:
            pass  # has its own bearer check in the route
        elif path in OPEN_PATHS or path.startswith("/static"):
            request.state.user_id = settings.default_user
        elif settings.mode == "private":
            bearer = request.headers.get("authorization", "")
            ok_bearer = bearer.startswith("Bearer ") and hmac.compare_digest(bearer[7:], settings.access_token or "")
            cookie = request.cookies.get(SESSION_COOKIE, "")
            ok_cookie = hmac.compare_digest(cookie, _session_value(settings.access_token or ""))
            if not (ok_bearer or ok_cookie):
                return deny(401, "Login required")
            request.state.user_id = settings.default_user
        elif settings.mode == "demo":
            uid = request.cookies.get(UID_COOKIE, "")
            if not UID_RE.match(uid):
                uid = new_uid = DEMO_PREFIX + secrets.token_hex(8)
            request.state.user_id = uid
        else:
            request.state.user_id = settings.default_user

        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001
            log.exception("unhandled error", extra={"path": path})
            response = JSONResponse({"error": "internal", "detail": "Something went wrong. Nothing was changed."},
                                    status_code=500)
        if new_uid:
            response.set_cookie(UID_COOKIE, new_uid, httponly=True, samesite="strict",
                                secure=settings.is_production, max_age=86400)
        return secure(response, path)

    # ------------------------------------------------------------------ errors
    def _err(status: int, code: str):
        async def handler(request: Request, exc: Exception):
            return JSONResponse({"error": code, "detail": str(exc)[:300]}, status_code=status)
        return handler

    for exc_cls, status, code in [
        (ActionNotFound, 404, "not_found"), (ActionConflict, 409, "conflict"), (ActionExpired, 410, "expired"),
        (InvalidAction, 422, "invalid_action"), (PayloadTampered, 409, "tampered"), (ChoiceError, 404, "not_found"),
        (BudgetExceeded, 429, "budget_exceeded"), (WriteToolBlocked, 403, "write_blocked"),
        (ApprovalRequired, 403, "approval_required"), (UpstreamError, 502, "upstream"), (NotConnected, 409, "not_connected"),
    ]:
        app.add_exception_handler(exc_cls, _err(status, code))

    def uid(request: Request) -> str:
        user = request.state.user_id
        ensure_user(ctx, user)
        return user

    def actor(request: Request) -> str:
        return "visitor" if settings.mode == "demo" else "owner"

    def page(request: Request, name: str, **kw):
        user = request.state.user_id
        pending = len(ctx.store.list_actions(user, status="pending")) if name != "login.html" else 0
        return templates.TemplateResponse(request, name, {
            "settings": settings, "pending_count": pending, "llm_mode": ctx.llm.mode,
            "data_mode": settings.effective_data_mode, "store_kind": ctx.store.kind,
            "version": VERSION, "active": name.split(".")[0], **kw})

    # ------------------------------------------------------------------ health
    @app.get("/health", tags=["ops"])
    def health():
        db_ok = ctx.store.ping()
        body = {"status": "ok" if db_ok else "degraded", "version": VERSION, "store": ctx.store.kind,
                "db": "up" if db_ok else "down", "data_mode": settings.effective_data_mode, "llm": ctx.llm.mode,
                "access_mode": settings.mode}
        return JSONResponse(body, status_code=200 if db_ok else 503)

    @app.get("/robots.txt", include_in_schema=False)
    def robots():
        return HTMLResponse("User-agent: *\nDisallow: /\n", media_type="text/plain")

    # ------------------------------------------------------------------ JSON API
    @app.post("/api/chat", tags=["assistant"])
    def api_chat(request: Request, body: ChatIn, user: str = Depends(uid)):
        return run_chat(ctx, user, body.message, actor=actor(request)).to_dict()

    @app.post("/api/briefing/run", tags=["assistant"])
    def api_briefing(request: Request, user: str = Depends(uid)):
        return run_briefing(ctx, user, actor=actor(request)).to_dict()

    @app.get("/api/briefing/latest", tags=["assistant"])
    def api_latest(user: str = Depends(uid)):
        p = ctx.store.latest_plan(user, "briefing")
        return {"plan": p["data"] if p else None, "plan_id": p["plan_id"] if p else None}

    @app.post("/api/jobs/briefing", tags=["ops"], include_in_schema=settings.mode != "demo")
    def api_cron(request: Request):
        """Called by a Railway cron job. Once per local day, idempotent."""
        tok = settings.cron_token
        bearer = request.headers.get("authorization", "")
        if settings.mode == "demo" or not tok or not (bearer.startswith("Bearer ") and hmac.compare_digest(bearer[7:], tok)):
            return JSONResponse({"error": "denied", "detail": "Not allowed"}, status_code=403)
        res = run_briefing_if_due(ctx, settings.default_user, actor="cron")
        return {"ran": res is not None, "result": res.to_dict() if res else None}

    @app.get("/api/dinner/{plan_id}", tags=["assistant"])
    def api_dinner(plan_id: str, user: str = Depends(uid)):
        row = ctx.store.get_plan(user, plan_id)
        if not row or row["kind"] != "dinner":
            raise ChoiceError("Plan not found")
        return row["data"]

    @app.post("/api/dinner/{plan_id}/choose", tags=["assistant"])
    def api_choose(plan_id: str, body: ChooseIn, user: str = Depends(uid)):
        return {"action_ids": choose_restaurant(ctx, user, plan_id, body.restaurant_id)}

    @app.get("/api/approvals", tags=["approvals"])
    def api_approvals(status: str | None = None, user: str = Depends(uid)):
        ctx.gate.expire_due(user)
        return [a.model_dump(mode="json") for a in ctx.store.list_actions(user, status=status)]

    @app.post("/api/approvals/{action_id}/approve", tags=["approvals"])
    def api_approve(request: Request, action_id: str, body: ApproveIn | None = None, user: str = Depends(uid)):
        a = ctx.gate.approve(user, action_id, actor(request), (body.edited_text if body else None))
        return a.model_dump(mode="json")

    @app.post("/api/approvals/{action_id}/reject", tags=["approvals"])
    def api_reject(request: Request, action_id: str, body: RejectIn | None = None, user: str = Depends(uid)):
        return ctx.gate.reject(user, action_id, actor(request), body.reason if body else None).model_dump(mode="json")

    @app.get("/api/audit", tags=["audit"])
    def api_audit(limit: int = 200, user: str = Depends(uid)):
        ok, bad = ctx.store.verify_audit(user)
        return {"chain_ok": ok, "first_bad_seq": bad, "entries": ctx.store.list_audit(user, min(limit, 1000))}

    @app.get("/api/me/data", tags=["privacy"])
    def api_export(user: str = Depends(uid)):
        return JSONResponse(jsonable_encoder(ctx.store.export_user_data(user)))

    @app.delete("/api/me/data", tags=["privacy"])
    def api_delete(user: str = Depends(uid)):
        counts = ctx.store.delete_user_data(user)
        return {"deleted": counts}

    @app.get("/api/memory", tags=["memory"])
    def api_memory(user: str = Depends(uid)):
        return {"preferences": ctx.store.get_prefs(user), "contacts": ctx.store.list_contacts(user)}

    @app.put("/api/memory/preferences", tags=["memory"])
    def api_put_prefs(body: PrefsIn, user: str = Depends(uid)):
        prefs = ctx.store.get_prefs(user)
        prefs.update({k: v for k, v in body.model_dump().items() if v not in (None, [])})
        ctx.store.set_prefs(user, prefs)
        ctx.store.append_audit(user, "preferences_updated", "owner", None, {"keys": sorted(prefs)})
        return prefs

    @app.post("/api/memory/contacts", tags=["memory"])
    def api_contact(body: ContactIn, user: str = Depends(uid)):
        ctx.store.upsert_contact(user, body.name, body.handle, body.notes)
        ctx.store.append_audit(user, "contact_saved", "owner", None, {"name": body.name})
        return {"ok": True}

    # ------------------------------------------------------------------ Google OAuth (private + google mode only)
    @app.get("/auth/google/start", tags=["google"], include_in_schema=settings.effective_data_mode == "google")
    def google_start():
        if not ctx.google or settings.mode != "private":
            return JSONResponse({"error": "disabled", "detail": "Google is only available in private mode."}, status_code=404)
        state = ctx.google.new_state()
        r = RedirectResponse(ctx.google.authorization_url(state), status_code=303)
        r.set_cookie("ea_oauth_state", state, httponly=True, samesite="lax", secure=settings.is_production, max_age=600)
        return r

    @app.get("/auth/google/callback", tags=["google"], include_in_schema=False)
    def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        if not ctx.google or settings.mode != "private":
            return JSONResponse({"error": "disabled"}, status_code=404)
        expected = request.cookies.get("ea_oauth_state", "")
        if error or not code or not expected or not hmac.compare_digest(expected, state):
            return JSONResponse({"error": "oauth_failed", "detail": error or "Invalid state"}, status_code=400)
        ctx.google.exchange_code(request.state.user_id, code)
        r = RedirectResponse("/memory?connected=1", status_code=303)
        r.delete_cookie("ea_oauth_state")
        return r

    # ------------------------------------------------------------------ pages
    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    def login_page(request: Request):
        if settings.mode != "private":
            return RedirectResponse("/", status_code=303)
        return page(request, "login.html", error=None)

    @app.post("/login", include_in_schema=False)
    def login(request: Request, token: str = Form(...)):
        client = request.client.host if request.client else "unknown"
        if not login_limiter.allow(client):
            return JSONResponse({"error": "rate_limited", "detail": "Too many attempts"}, status_code=429)
        if settings.mode == "private" and hmac.compare_digest(token, settings.access_token or ""):
            r = RedirectResponse("/", status_code=303)
            # Lax (not Strict) so the cookie survives the redirect back from Google's consent screen.
            # Cross-site POSTs are still refused by the Origin check in the middleware.
            r.set_cookie(SESSION_COOKIE, _session_value(settings.access_token or ""), httponly=True,
                         samesite="lax", secure=settings.is_production, max_age=60 * 60 * 24 * 14)
            return r
        return page(request, "login.html", error="That token is not valid.")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home(request: Request, user: str = Depends(uid)):
        return page(request, "home.html", messages=ctx.store.list_messages(user, 30),
                    google_connected=bool(ctx.google and ctx.google.connected(user)))

    @app.post("/ui/chat", response_class=HTMLResponse, include_in_schema=False)
    def ui_chat(request: Request, message: str = Form(..., max_length=500), user: str = Depends(uid)):
        try:
            res = run_chat(ctx, user, message, actor=actor(request))
        except BudgetExceeded as exc:
            return templates.TemplateResponse(request, "_turn.html", {"user_text": message, "reply": str(exc), "res": None})
        plan_id = res.plan_id
        return templates.TemplateResponse(request, "_turn.html", {
            "user_text": message, "reply": res.reply, "res": res, "plan_id": plan_id})

    @app.get("/plan", response_class=HTMLResponse, include_in_schema=False)
    def plan_page(request: Request, user: str = Depends(uid)):
        p = ctx.store.latest_plan(user, "briefing")
        return page(request, "plan.html", plan=p["data"] if p else None, created=p["created_at"] if p else None)

    @app.post("/ui/briefing", include_in_schema=False)
    def ui_briefing(request: Request, user: str = Depends(uid)):
        run_briefing(ctx, user, actor=actor(request))
        return RedirectResponse("/plan", status_code=303)

    @app.get("/dinner/{plan_id}", response_class=HTMLResponse, include_in_schema=False)
    def dinner_page(request: Request, plan_id: str, user: str = Depends(uid)):
        row = ctx.store.get_plan(user, plan_id)
        if not row or row["kind"] != "dinner":
            return RedirectResponse("/approvals", status_code=303)
        plan = DinnerPlan.model_validate(row["data"]["plan"])
        actions = ctx.store.list_actions(user, flow_id=plan_id)
        live = next((a for a in actions if a.kind == "book_table" and a.status in ("pending", "executed")), None)
        return page(request, "dinner.html", plan=plan, plan_id=plan_id, chosen=(live.payload["restaurant_id"] if live else None))

    @app.post("/ui/dinner/{plan_id}/choose", include_in_schema=False)
    def ui_choose(plan_id: str, restaurant_id: str = Form(...), user: str = Depends(uid)):
        choose_restaurant(ctx, user, plan_id, restaurant_id)
        return RedirectResponse("/approvals", status_code=303)

    @app.get("/approvals", response_class=HTMLResponse, include_in_schema=False)
    def approvals_page(request: Request, user: str = Depends(uid)):
        ctx.gate.expire_due(user)
        pending = ctx.store.list_actions(user, status="pending")
        history = [a for a in ctx.store.list_actions(user, limit=40) if a.status != "pending"]
        return page(request, "approvals.html", pending=pending, history=history)

    def card(request: Request, a, error: str | None = None):
        return templates.TemplateResponse(request, "_action_card.html", {"a": a, "error": error, "settings": settings})

    @app.post("/ui/approvals/{action_id}/approve", response_class=HTMLResponse, include_in_schema=False)
    def ui_approve(request: Request, action_id: str, edited_text: str = Form(default=""), user: str = Depends(uid)):
        try:
            a = ctx.gate.approve(user, action_id, actor(request), edited_text.strip() or None)
            return card(request, a)
        except (ActionConflict, ActionExpired, InvalidAction, PayloadTampered, ApprovalRequired) as exc:
            return card(request, ctx.store.get_action(user, action_id), str(exc))

    @app.post("/ui/approvals/{action_id}/reject", response_class=HTMLResponse, include_in_schema=False)
    def ui_reject(request: Request, action_id: str, user: str = Depends(uid)):
        try:
            return card(request, ctx.gate.reject(user, action_id, actor(request)))
        except ActionConflict as exc:
            return card(request, ctx.store.get_action(user, action_id), str(exc))

    @app.get("/audit", response_class=HTMLResponse, include_in_schema=False)
    def audit_page(request: Request, hide_reads: int = 0, user: str = Depends(uid)):
        ok, bad = ctx.store.verify_audit(user)
        entries = ctx.store.list_audit(user, 300)
        if hide_reads:
            entries = [e for e in entries if e["event"] not in ("tool_read",)]
        return page(request, "audit.html", entries=entries, chain_ok=ok, bad_seq=bad, hide_reads=hide_reads)

    @app.get("/memory", response_class=HTMLResponse, include_in_schema=False)
    def memory_page(request: Request, user: str = Depends(uid)):
        return page(request, "memory.html", prefs=ctx.store.get_prefs(user), contacts=ctx.store.list_contacts(user),
                    google_connected=bool(ctx.google and ctx.google.connected(user)), saved=request.query_params.get("saved"))

    @app.post("/ui/memory/contact", include_in_schema=False)
    def ui_contact(name: str = Form(..., max_length=60), handle: str = Form(default="", max_length=120),
                   notes: str = Form(default="", max_length=200), user: str = Depends(uid)):
        ctx.store.upsert_contact(user, name.strip(), handle.strip() or None, notes.strip() or None)
        ctx.store.append_audit(user, "contact_saved", "owner", None, {"name": name.strip()})
        return RedirectResponse("/memory?saved=contact", status_code=303)

    @app.post("/ui/memory/prefs", include_in_schema=False)
    def ui_prefs(home_area: str = Form(default="", max_length=60), favorite_cuisines: str = Form(default="", max_length=200),
                 max_price_level: str = Form(default=""), vip_senders: str = Form(default="", max_length=200),
                 user: str = Depends(uid)):
        prefs = ctx.store.get_prefs(user)
        split = lambda s: [x.strip() for x in s.split(",") if x.strip()][:8]  # noqa: E731
        prefs["home_area"] = home_area.strip()
        prefs["favorite_cuisines"] = split(favorite_cuisines)
        prefs["vip_senders"] = split(vip_senders)
        if max_price_level.isdigit() and 1 <= int(max_price_level) <= 4:
            prefs["max_price_level"] = int(max_price_level)
        ctx.store.set_prefs(user, prefs)
        ctx.store.append_audit(user, "preferences_updated", "owner", None, {"keys": sorted(prefs)})
        return RedirectResponse("/memory?saved=prefs", status_code=303)

    @app.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
    def privacy_page(request: Request, user: str = Depends(uid)):
        data = ctx.store.export_user_data(user)
        counts = {k: (len(v) if isinstance(v, (list, dict)) else 1) for k, v in data.items()}
        return page(request, "privacy.html", counts=counts)

    @app.post("/ui/delete-my-data", include_in_schema=False)
    def ui_delete(request: Request, confirm: str = Form(default=""), user: str = Depends(uid)):
        if confirm != "yes":
            return RedirectResponse("/privacy", status_code=303)
        ctx.store.delete_user_data(user)
        r = RedirectResponse("/", status_code=303)
        if settings.mode == "demo":
            r.delete_cookie(UID_COOKIE)
        return r

    return app


def _default_app() -> FastAPI:
    return create_app()


app = _default_app()
