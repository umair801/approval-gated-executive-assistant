"""Background jobs: the daily briefing and the demo-sandbox purge. Also callable from a cron URL."""
from __future__ import annotations

import asyncio
from datetime import timedelta

from .analysis import parse_hhmm
from .context import AppContext
from .flows import run_briefing_if_due
from .logging_setup import get_logger

log = get_logger("scheduler")
DEMO_PREFIX = "demo-"


def purge_demo_users(ctx: AppContext, max_age_hours: int = 24) -> list[str]:
    cutoff = ctx.clock() - timedelta(hours=max_age_hours)
    gone = ctx.store.purge_stale_users(DEMO_PREFIX, cutoff)
    if gone:
        log.info("purged demo sandboxes", extra={"count": len(gone)})
    return gone


def briefing_tick(ctx: AppContext) -> bool:
    """Run today's briefing if the configured time has passed. Returns True if one ran."""
    if ctx.settings.mode == "demo":
        return False
    now_local = ctx.clock().astimezone(ctx.tz)
    if now_local.time() < parse_hhmm(ctx.settings.briefing_time):
        return False
    res = run_briefing_if_due(ctx, ctx.settings.default_user, actor="scheduler")
    return res is not None


async def scheduler_loop(ctx: AppContext, interval_s: int = 60) -> None:
    ticks = 0
    while True:
        try:
            if ctx.settings.briefing_enabled:
                await asyncio.to_thread(briefing_tick, ctx)
            if ticks % 60 == 0 and ctx.settings.mode == "demo":
                await asyncio.to_thread(purge_demo_users, ctx)
        except Exception:  # noqa: BLE001  the loop must survive any single failure
            log.exception("scheduler tick failed")
        ticks += 1
        await asyncio.sleep(interval_s)
