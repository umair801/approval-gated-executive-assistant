"""Google Calendar and Gmail with READ-ONLY scopes, and optional Places (New) search.

Data minimization: Gmail is read with format=metadata (sender, subject, date, snippet). Message
bodies and attachments are never requested. Refresh tokens are encrypted before storage.

Live-account behavior needs your own Google Cloud OAuth client. See docs/GOOGLE_SETUP.md.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from ..config import Settings
from ..crypto import TokenVault
from ..logging_setup import get_logger
from ..models import CalendarEvent, EmailSummary, Restaurant
from ..net import UpstreamError, request_with_retry
from ..store import Store

log = get_logger("google")

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
]
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CAL_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
GMAIL_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"


class NotConnected(Exception):
    pass


class GoogleClient:
    def __init__(self, settings: Settings, store: Store, vault: TokenVault,
                 http: httpx.Client | None = None, sleep=None) -> None:
        self.s, self.store, self.vault = settings, store, vault
        self.http = http or httpx.Client(timeout=settings.http_timeout_s)
        self._sleep = sleep
        self._access: dict[str, tuple[str, datetime]] = {}

    # ---- OAuth
    def new_state(self) -> str:
        return secrets.token_urlsafe(24)

    def authorization_url(self, state: str) -> str:
        q = {"client_id": self.s.google_client_id, "redirect_uri": self.s.google_redirect_uri,
             "response_type": "code", "scope": " ".join(SCOPES), "access_type": "offline",
             "prompt": "consent", "state": state, "include_granted_scopes": "true"}
        return f"{AUTH_URL}?{urlencode(q)}"

    def exchange_code(self, user_id: str, code: str) -> None:
        r = self._send(lambda: self.http.post(TOKEN_URL, data={
            "code": code, "client_id": self.s.google_client_id, "client_secret": self.s.google_client_secret,
            "redirect_uri": self.s.google_redirect_uri, "grant_type": "authorization_code"}), "token")
        data = r.json()
        refresh = data.get("refresh_token")
        if not refresh:
            raise UpstreamError("Google did not return a refresh token. Revoke access and connect again.")
        granted = set((data.get("scope") or "").split())
        if not granted <= set(SCOPES) | {"openid"}:
            raise UpstreamError("Google granted scopes beyond read-only. Refusing to store the token.")
        self.store.save_token(user_id, "google", self.vault.encrypt(refresh), data.get("scope"))
        self._access[user_id] = (data["access_token"], datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 3000)))
        self.store.append_audit(user_id, "google_connected", "owner", None, {"scopes": data.get("scope")})

    def connected(self, user_id: str) -> bool:
        return self.store.get_token(user_id, "google") is not None

    def _access_token(self, user_id: str) -> str:
        cached = self._access.get(user_id)
        if cached and cached[1] > datetime.now(timezone.utc) + timedelta(seconds=60):
            return cached[0]
        cipher = self.store.get_token(user_id, "google")
        if cipher is None:
            raise NotConnected("Google account is not connected")
        refresh = self.vault.decrypt(cipher)
        r = self._send(lambda: self.http.post(TOKEN_URL, data={
            "client_id": self.s.google_client_id, "client_secret": self.s.google_client_secret,
            "refresh_token": refresh, "grant_type": "refresh_token"}), "token")
        data = r.json()
        self._access[user_id] = (data["access_token"], datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 3000)))
        return data["access_token"]

    def _send(self, fn, what: str):
        kw = {"sleep": self._sleep} if self._sleep else {}
        r = request_with_retry(fn, what=what, **kw)
        if r.status_code >= 400:
            raise UpstreamError(f"{what} failed with {r.status_code}", r.status_code)
        return r

    def _get(self, user_id: str, url: str, params: dict, what: str) -> dict:
        tok = self._access_token(user_id)
        return self._send(lambda: self.http.get(url, params=params, headers={"Authorization": f"Bearer {tok}"}), what).json()

    # ---- Calendar
    def events(self, user_id: str, start: datetime, end: datetime) -> list[CalendarEvent]:
        data = self._get(user_id, CAL_URL, {
            "timeMin": start.astimezone(timezone.utc).isoformat(), "timeMax": end.astimezone(timezone.utc).isoformat(),
            "singleEvents": "true", "orderBy": "startTime", "maxResults": 100}, "calendar")
        out: list[CalendarEvent] = []
        for it in data.get("items", []):
            if it.get("status") == "cancelled":
                continue
            s, e = it.get("start", {}), it.get("end", {})
            all_day = "date" in s
            try:
                st = datetime.fromisoformat(s["dateTime"] if not all_day else s["date"] + "T00:00:00+00:00")
                en = datetime.fromisoformat(e["dateTime"] if not all_day else e["date"] + "T00:00:00+00:00")
            except (KeyError, ValueError):
                continue
            out.append(CalendarEvent(
                id=it["id"], title=(it.get("summary") or "(no title)")[:120], start=st, end=en,
                location=(it.get("location") or None), all_day=all_day,
                attendees=[a.get("displayName") or a.get("email", "") for a in it.get("attendees", [])][:10]))
        return out

    # ---- Gmail (metadata only)
    def recent(self, user_id: str, since: datetime) -> list[EmailSummary]:
        days = max(1, int((datetime.now(timezone.utc) - since).total_seconds() // 86400) + 1)
        listing = self._get(user_id, GMAIL_URL, {"q": f"in:inbox newer_than:{days}d", "maxResults": 15}, "gmail")
        out: list[EmailSummary] = []
        for m in listing.get("messages", []):
            d = self._get(user_id, f"{GMAIL_URL}/{m['id']}", {
                "format": "metadata", "metadataHeaders": ["From", "Subject"]}, "gmail")
            hdr = {h["name"].lower(): h["value"] for h in d.get("payload", {}).get("headers", [])}
            received = datetime.fromtimestamp(int(d.get("internalDate", "0")) / 1000, tz=timezone.utc)
            if received < since:
                continue
            out.append(EmailSummary(
                id=m["id"], sender=hdr.get("from", "unknown")[:120], subject=hdr.get("subject", "(no subject)")[:160],
                snippet=(d.get("snippet") or "")[:240], received=received, unread="UNREAD" in d.get("labelIds", [])))
        return out


class GooglePlaces:
    """Places API (New) text search. Needs PLACES_API_KEY."""
    _PRICE = {"PRICE_LEVEL_INEXPENSIVE": 1, "PRICE_LEVEL_MODERATE": 2, "PRICE_LEVEL_EXPENSIVE": 3,
              "PRICE_LEVEL_VERY_EXPENSIVE": 4}

    def __init__(self, api_key: str, http: httpx.Client | None = None, timeout: float = 15.0, sleep=None) -> None:
        self.key, self.http, self._sleep = api_key, http or httpx.Client(timeout=timeout), sleep

    def search(self, user_id: str, cuisine: str | None, area: str | None) -> list[Restaurant]:
        query = f"{cuisine or ''} restaurants" + (f" in {area}" if area else "")
        kw = {"sleep": self._sleep} if self._sleep else {}
        r = request_with_retry(lambda: self.http.post(PLACES_URL, json={"textQuery": query.strip(), "maxResultCount": 12},
            headers={"X-Goog-Api-Key": self.key, "X-Goog-FieldMask":
                     "places.id,places.displayName,places.rating,places.priceLevel,places.formattedAddress,"
                     "places.googleMapsUri,places.primaryTypeDisplayName"}), what="places", **kw)
        if r.status_code >= 400:
            raise UpstreamError(f"places failed with {r.status_code}", r.status_code)
        out = []
        for p in r.json().get("places", []):
            out.append(Restaurant(
                id=p["id"], name=p.get("displayName", {}).get("text", "Unknown")[:80],
                cuisine=(p.get("primaryTypeDisplayName", {}).get("text") or cuisine or "Restaurant"),
                price_level=self._PRICE.get(p.get("priceLevel", ""), 2), rating=float(p.get("rating", 0) or 0),
                neighborhood=(p.get("formattedAddress") or "")[:60], distance_km=0.0,
                link=p.get("googleMapsUri"), source="places"))
        return out


class GoogleCalendarAdapter:
    def __init__(self, g: GoogleClient) -> None:
        self.g = g

    def events(self, user_id, start, end):
        return self.g.events(user_id, start, end)


class GoogleEmailAdapter:
    def __init__(self, g: GoogleClient) -> None:
        self.g = g

    def recent(self, user_id, since):
        return self.g.recent(user_id, since)
