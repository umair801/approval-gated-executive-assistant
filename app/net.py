"""Retry with backoff for upstream calls. Honors Retry-After on 429."""
from __future__ import annotations

import random
import time
from typing import Callable, TypeVar

import httpx

from .logging_setup import get_logger

log = get_logger("net")
T = TypeVar("T")

RETRY_STATUS = {429, 500, 502, 503, 504}


class UpstreamError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def request_with_retry(
    send: Callable[[], httpx.Response],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    what: str = "upstream",
) -> httpx.Response:
    last: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            resp = send()
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = exc
            log.warning("transport error", extra={"what": what, "attempt": i})
        else:
            if resp.status_code not in RETRY_STATUS:
                return resp
            last = UpstreamError(f"{what} returned {resp.status_code}", resp.status_code)
            retry_after = resp.headers.get("Retry-After")
            if retry_after and retry_after.isdigit() and i < attempts:
                log.warning("rate limited", extra={"what": what, "attempt": i, "retry_after": retry_after})
                sleep(min(float(retry_after), 10.0))
                continue
            log.warning("retryable status", extra={"what": what, "attempt": i, "status": resp.status_code})
        if i < attempts:
            sleep(base_delay * (2 ** (i - 1)) + random.uniform(0, 0.1))
    if isinstance(last, UpstreamError):
        raise last
    raise UpstreamError(f"{what} unreachable: {type(last).__name__}")
