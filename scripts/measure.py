"""Measure the numbers the case study is allowed to quote. Nothing here is estimated.

    python scripts/measure.py --runs 30
    python scripts/measure.py --runs 30 --database-url postgresql://...   # include your database
    ANTHROPIC_API_KEY=... PRICE_IN_PER_MTOK=.. PRICE_OUT_PER_MTOK=.. python scripts/measure.py --runs 10

Output: eval/results/metrics.json. Then run: python scripts/render_case_study.py
"""
from __future__ import annotations

import argparse
import json
import platform
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings  # noqa: E402
from app.context import build_context  # noqa: E402
from app.executors import ActionExecutor, ApprovalRequired, PayloadTampered  # noqa: E402
from app.flows import run_briefing, run_chat  # noqa: E402
from app.logging_setup import setup_logging  # noqa: E402
from app.store import PostgresStore  # noqa: E402
from app.tools.registry import WriteToolBlocked  # noqa: E402

WRITE_KINDS = {"send_message", "book_table", "pay"}


def pct(values: list[float], p: float) -> float:
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 2)


def summarize(values: list[float]) -> dict:
    return {"n": len(values), "mean": round(statistics.fmean(values), 2), "p50": pct(values, .5),
            "p95": pct(values, .95), "min": round(min(values), 2), "max": round(max(values), 2)}


def run_tests() -> dict:
    t0 = time.perf_counter()
    # pytest.ini already adds -q; a second -q would hide the summary line parsed below.
    p = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider"], cwd=ROOT,
                       capture_output=True, text=True)
    out = p.stdout + p.stderr
    passed = int((re.search(r"(\d+) passed", out) or [0, 0])[1])
    failed = int((re.search(r"(\d+) failed", out) or [0, 0])[1])
    skipped = int((re.search(r"(\d+) skipped", out) or [0, 0])[1])
    return {"passed": passed, "failed": failed, "skipped": skipped, "seconds": round(time.perf_counter() - t0, 1),
            "exit_code": p.returncode}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=30)
    ap.add_argument("--database-url", default=None)
    ap.add_argument("--out", default=str(ROOT / "eval" / "results" / "metrics.json"))
    ap.add_argument("--skip-tests", action="store_true")
    a = ap.parse_args()

    setup_logging()  # show the real reason when a model call fails
    base = load_settings()
    settings = base.__class__(**{**base.__dict__, "database_url": a.database_url or base.database_url,
                                 "rate_limit_per_min": 10_000, "daily_llm_call_cap": 10_000})
    ctx = build_context(settings)
    if isinstance(ctx.store, PostgresStore):  # clean slate for measurement users only
        for t in ("audit_log", "outbox", "approvals", "actions", "messages", "plans", "contacts", "preferences",
                  "app_users", "oauth_tokens"):
            ctx.store._exec(f"delete from {t} where user_id like %s", ("measure-%",))

    brief, dinner = [], []
    cost_brief, tokens_brief, calls_brief = [], [], []
    cost_dinner, calls_dinner = [], []
    proposed = executed = executed_with_approval = 0
    attempts = blocked = 0
    users = []

    for i in range(a.runs):
        u = f"measure-{i}"
        users.append(u)
        b = run_briefing(ctx, u, actor="measure")
        if not b.ok:
            raise SystemExit(f"Briefing failed: {b.reply}\nSee the 'flow failed' log line above for the reason. Nothing was measured.")
        brief.append(b.latency_ms); calls_brief.append(b.llm_calls); tokens_brief.append(b.tokens_in + b.tokens_out)
        if b.cost_usd is not None:
            cost_brief.append(b.cost_usd)

        d = run_chat(ctx, u, "Plan dinner with Sam on Friday", actor="measure")
        if not d.ok:
            raise SystemExit(f"Dinner flow failed: {d.reply}\nSee the 'flow failed' log line above for the reason. Nothing was measured.")
        dinner.append(d.latency_ms); calls_dinner.append(d.llm_calls)
        if d.cost_usd is not None:
            cost_dinner.append(d.cost_usd)

        pending = ctx.store.list_actions(u, status="pending")
        proposed += len(pending)

        # Bypass attempts BEFORE approval: every one must fail and leave no side effect.
        probe = pending[0]
        for attempt, exc in (
            (lambda: ActionExecutor(ctx.store).run(probe), ApprovalRequired),
            (lambda: ctx.registry.call(u, "send_message", to="x", text="y"), WriteToolBlocked),
            (lambda: ctx.registry.call(u, "book_table"), WriteToolBlocked),
        ):
            attempts += 1
            try:
                attempt()
            except exc:
                blocked += 1
        assert ctx.store.list_outbox(u) == [], "side effect without approval"

        for act in pending:
            ctx.gate.approve(u, act.action_id, "measure")
        # Tampered payload after approval must be refused.
        done = ctx.store.get_action(u, pending[0].action_id)
        attempts += 1
        try:
            ActionExecutor(ctx.store).run(done.model_copy(update={"payload": {**done.payload, "x": 1}}))
        except PayloadTampered:
            blocked += 1

        for act in ctx.store.list_actions(u):
            if act.kind in WRITE_KINDS and act.status == "executed":
                executed += 1
                if ctx.store.get_approval(act.action_id):
                    executed_with_approval += 1
        assert ctx.store.verify_audit(u) == (True, None)

    tests = None if a.skip_tests else run_tests()
    price_set = settings.price_in_per_mtok is not None and settings.price_out_per_mtok is not None
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "store": ctx.store.kind, "data_mode": settings.effective_data_mode,
                        "llm_mode": ctx.llm.mode, "model": settings.claude_model if ctx.llm.mode == "claude" else None,
                        "runs": a.runs},
        "latency_ms": {"morning_briefing": summarize(brief), "plan_a_dinner": summarize(dinner)},
        "latency_includes_model_time": ctx.llm.mode == "claude",
        "llm_calls_per_flow": {"morning_briefing": statistics.fmean(calls_brief), "plan_a_dinner": statistics.fmean(calls_dinner)},
        "cost_per_briefing_usd": (round(statistics.fmean(cost_brief), 6) if cost_brief else None),
        "cost_per_dinner_plan_usd": (round(statistics.fmean(cost_dinner), 6) if cost_dinner else None),
        "cost_note": (None if cost_brief else
                      ("Not measured: run with ANTHROPIC_API_KEY to use Claude." if ctx.llm.mode == "mock"
                       else "Not measured: set PRICE_IN_PER_MTOK and PRICE_OUT_PER_MTOK from your price list.")),
        "tokens_per_briefing": (round(statistics.fmean(tokens_brief)) if ctx.llm.mode == "claude" else None),
        "approval": {
            "write_actions_proposed": proposed,
            "write_actions_executed": executed,
            "executed_with_recorded_approval": executed_with_approval,
            "approval_coverage_pct": round(100 * executed_with_approval / executed, 2) if executed else None,
            "bypass_attempts": attempts, "bypass_attempts_blocked": blocked,
            "audit_chains_verified": len(users),
        },
        "tests": tests,
        "price_configured": price_set,
    }
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    ok = (result["approval"]["approval_coverage_pct"] == 100.0 and attempts == blocked
          and (tests is None or (tests["failed"] == 0 and tests["passed"] > 0 and tests["exit_code"] == 0)))
    print("\nRESULT:", "OK" if ok else "CHECK FAILED")
    close = getattr(ctx.store, "close", None)
    if close:
        close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
