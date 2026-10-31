"""Google clients against a mocked transport. These prove request shape and parsing, not live behavior."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet

from app.config import Settings
from app.crypto import TokenVault, VaultError
from app.net import UpstreamError
from app.store import MemoryStore
from app.tools.google import SCOPES, GoogleClient, GooglePlaces

S = Settings(google_client_id="cid", google_client_secret="sec", data_mode="google", access_token="t",
             token_encryption_key=Fernet.generate_key().decode())
U = "owner"
NOW = datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc)


def make(handler, store=None):
    store = store or MemoryStore()
    vault = TokenVault(S.token_encryption_key)
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return GoogleClient(S, store, vault, http, sleep=lambda s: None), store, vault


def token_ok(scope=" ".join(SCOPES), refresh="rt-123"):
    body = {"access_token": "at-1", "expires_in": 3600, "scope": scope}
    if refresh:
        body["refresh_token"] = refresh
    return httpx.Response(200, json=body)


def test_authorization_url_requests_only_read_only_scopes():
    g, _, _ = make(lambda r: token_ok())
    q = parse_qs(urlparse(g.authorization_url("st")).query)
    assert set(q["scope"][0].split()) == set(SCOPES)
    assert all(s.endswith(".readonly") for s in SCOPES)
    assert q["access_type"] == ["offline"] and q["state"] == ["st"]


def test_refresh_token_is_encrypted_at_rest():
    g, store, vault = make(lambda r: token_ok())
    g.exchange_code(U, "code")
    stored = store.get_token(U, "google")
    assert stored and "rt-123" not in stored
    assert vault.decrypt(stored) == "rt-123"
    assert "google_connected" in [e["event"] for e in store.list_audit(U)]


def test_token_with_broader_scope_is_refused_and_not_stored():
    g, store, _ = make(lambda r: token_ok(scope="https://www.googleapis.com/auth/gmail.modify"))
    with pytest.raises(UpstreamError):
        g.exchange_code(U, "code")
    assert store.get_token(U, "google") is None


def test_missing_refresh_token_is_an_error():
    g, store, _ = make(lambda r: token_ok(refresh=None))
    with pytest.raises(UpstreamError):
        g.exchange_code(U, "code")


def test_vault_rejects_wrong_key():
    a, b = TokenVault(Fernet.generate_key().decode()), TokenVault(Fernet.generate_key().decode())
    with pytest.raises(VaultError):
        b.decrypt(a.encrypt("x"))
    with pytest.raises(VaultError):
        TokenVault("not-a-key")


def test_calendar_events_parsed_and_cancelled_skipped():
    def handler(req):
        if req.url.host == "oauth2.googleapis.com":
            return token_ok()
        assert req.headers["authorization"] == "Bearer at-1"
        return httpx.Response(200, json={"items": [
            {"id": "a", "summary": "Standup", "start": {"dateTime": "2026-10-14T09:00:00+08:00"},
             "end": {"dateTime": "2026-10-14T09:30:00+08:00"}, "attendees": [{"displayName": "Priya"}]},
            {"id": "b", "summary": "Holiday", "start": {"date": "2026-10-15"}, "end": {"date": "2026-10-16"}},
            {"id": "c", "status": "cancelled", "start": {"dateTime": "2026-10-14T10:00:00+08:00"},
             "end": {"dateTime": "2026-10-14T11:00:00+08:00"}}]})
    g, store, vault = make(handler)
    store.save_token(U, "google", vault.encrypt("rt"), None)
    ev = g.events(U, NOW, NOW + timedelta(days=1))
    assert [e.id for e in ev] == ["a", "b"] and ev[1].all_day and ev[0].attendees == ["Priya"]


def test_gmail_reads_metadata_only_never_bodies():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        if req.url.host == "oauth2.googleapis.com":
            return token_ok()
        if req.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "m1"}]})
        return httpx.Response(200, json={"id": "m1", "snippet": "Hi there", "labelIds": ["UNREAD"],
            "internalDate": str(int((NOW - timedelta(hours=1)).timestamp() * 1000)),
            "payload": {"headers": [{"name": "From", "value": "A <a@example.com>"}, {"name": "Subject", "value": "Hello"}]}})
    g, store, vault = make(handler)
    store.save_token(U, "google", vault.encrypt("rt"), None)
    mails = g.recent(U, NOW - timedelta(hours=48))
    assert mails[0].subject == "Hello" and mails[0].unread
    detail = [u for u in seen if "/messages/m1" in u][0]
    assert "format=metadata" in detail and "format=full" not in detail and "raw" not in detail


def test_429_with_retry_after_is_retried_then_succeeds():
    calls = {"n": 0}

    def handler(req):
        if req.url.host == "oauth2.googleapis.com":
            return token_ok()
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"items": []})
    g, store, vault = make(handler)
    store.save_token(U, "google", vault.encrypt("rt"), None)
    assert g.events(U, NOW, NOW + timedelta(days=1)) == [] and calls["n"] == 2


def test_persistent_server_error_surfaces_as_upstream_error():
    def handler(req):
        return token_ok() if req.url.host == "oauth2.googleapis.com" else httpx.Response(503)
    g, store, vault = make(handler)
    store.save_token(U, "google", vault.encrypt("rt"), None)
    with pytest.raises(UpstreamError):
        g.events(U, NOW, NOW + timedelta(days=1))


def test_places_search_maps_results():
    def handler(req):
        assert req.headers["x-goog-api-key"] == "k" and "places.id" in req.headers["x-goog-fieldmask"]
        body = json.loads(req.content)
        assert "Italian" in body["textQuery"]
        return httpx.Response(200, json={"places": [{"id": "p1", "displayName": {"text": "Da Mario"}, "rating": 4.5,
            "priceLevel": "PRICE_LEVEL_MODERATE", "formattedAddress": "1 Example St", "googleMapsUri": "https://maps.example/p1",
            "primaryTypeDisplayName": {"text": "Italian restaurant"}}]})
    p = GooglePlaces("k", httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    out = p.search(U, "Italian", "Riverside")
    assert out[0].name == "Da Mario" and out[0].price_level == 2 and out[0].source == "places"
