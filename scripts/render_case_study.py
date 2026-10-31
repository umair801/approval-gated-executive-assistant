"""Fill docs/case_study.template.md from eval/results/metrics.json. Refuses to leave a placeholder
unfilled, so no number can appear that was not measured.

    python scripts/render_case_study.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    m = json.loads((ROOT / "eval" / "results" / "metrics.json").read_text())
    a, lat, env, t = m["approval"], m["latency_ms"], m["environment"], m["tests"]
    if not t or not t["passed"]:
        print("metrics.json has no test results. Run: python scripts/measure.py"); return 1
    claude = env["llm_mode"] == "claude"
    cost = m["cost_per_briefing_usd"]
    tests_line = f"{t['passed']} passed, {t['failed']} failed" + (f", {t['skipped']} skipped" if t["skipped"] else "")
    if env["store"] == "postgres":
        tests_line += " (suite run against PostgreSQL; the memory-only run has fewer because some tests are store-specific)"
    scope = (
        "Measured with the real Claude model, so they include model response time."
        if claude else
        f"Measured with the built-in deterministic model and a {env['store']} store, so they cover orchestration, "
        "data access, analysis and the approval logic, but **not** model response time. Rerun "
        "`scripts/measure.py` with `ANTHROPIC_API_KEY` set to measure end-to-end latency.")
    vals = {
        "runs": env["runs"], "generated_at": m["generated_at"][:10],
        "approval_coverage_pct": a["approval_coverage_pct"], "executed_with_approval": a["executed_with_recorded_approval"],
        "executed": a["write_actions_executed"], "unapproved": a["write_actions_executed"] - a["executed_with_recorded_approval"],
        "bypass_blocked": a["bypass_attempts_blocked"], "bypass_attempts": a["bypass_attempts"],
        "brief_p50": lat["morning_briefing"]["p50"], "brief_p95": lat["morning_briefing"]["p95"],
        "dinner_p50": lat["plan_a_dinner"]["p50"], "dinner_p95": lat["plan_a_dinner"]["p95"],
        "calls_brief": m["llm_calls_per_flow"]["morning_briefing"], "calls_dinner": m["llm_calls_per_flow"]["plan_a_dinner"],
        "cost_brief": (f"${cost}" if cost is not None else "Not measured yet. It needs a run with a real model key and your price settings."),
        "tests_line": tests_line, "chains": a["audit_chains_verified"], "latency_scope": scope,
    }
    text = (ROOT / "docs" / "case_study.template.md").read_text()
    for k, v in vals.items():
        text = text.replace("{{" + k + "}}", str(v))
    left = re.findall(r"\{\{(\w+)\}\}", text)
    if left:
        print("Unfilled placeholders:", left); return 1
    (ROOT / "docs" / "case_study.md").write_text(text)
    print("Wrote docs/case_study.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
