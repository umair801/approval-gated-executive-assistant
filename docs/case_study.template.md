# Case study: an executive assistant that cannot act without you

## The problem

People want an assistant that handles the busywork of scheduling, planning and messaging. They do not
want one that can send a message, book a table or move money on its own. Most assistant demos skip
that tension. This project is built around it.

## The approach

Two flows run through one gate.

1. **Morning briefing.** A scheduled job reads the calendar and recent mail, finds overlaps,
   back-to-back meetings and overloaded days, ranks the mail that needs attention and sends one plan.
2. **Plan a dinner.** The assistant finds free evening slots, shortlists three restaurants, drafts a
   message to the friend and prepares a booking.

Reads run automatically. Anything that sends, books or pays becomes a **proposal** in an approval
queue. Nothing happens until the user approves, and every step lands in a hash-chained audit log.

## How the gate works

- The agent has no executable write tools. Calling one directly is blocked and logged.
- Approving records a unique approval row, then runs the action once. Double clicks, retries and
  concurrent requests cannot run it twice.
- The executor re-checks the recorded approval and the payload hash before doing anything, so a missing
  approval or a changed message is refused even if an earlier check had a bug.
- Proposals expire. Edited messages are re-hashed and the edit is audited.
- Email text is treated as data. Mail that reads like instructions to an AI is flagged, excluded and reported.

## Results (measured)

All numbers below come from `scripts/measure.py` ({{runs}} runs per flow, {{generated_at}}) and the test suite.
Nothing is estimated.

| Metric | Result |
|---|---|
| Approval coverage of write actions | **{{approval_coverage_pct}}%** ({{executed_with_approval}} of {{executed}} executed writes had a recorded approval) |
| Write actions executed without approval | **{{unapproved}}** |
| Bypass attempts blocked | **{{bypass_blocked}} of {{bypass_attempts}}** (direct write-tool calls, executor calls without approval, payload tampering) |
| Morning briefing latency | p50 **{{brief_p50}} ms**, p95 {{brief_p95}} ms |
| Plan-a-dinner latency | p50 **{{dinner_p50}} ms**, p95 {{dinner_p95}} ms |
| Model calls per flow | briefing {{calls_brief}}, dinner {{calls_dinner}} |
| Cost per briefing | {{cost_brief}} |
| Automated tests | **{{tests_line}}** |
| Audit chains verified | {{chains}} of {{runs}} runs |

**Scope of the latency numbers.** {{latency_scope}}

## Where this pattern fits

Any workflow where software prepares work and a person must authorize the consequential step:
personal and executive assistants, scheduling and travel, outbound messaging with compliance review,
procurement and invoice approval, and customer communication drafted by an agent.

## Stack

LangGraph, Claude (forced tool call + Pydantic validation), FastAPI, Jinja2 and HTMX, Supabase / PostgreSQL,
Docker, Railway. Google Calendar and Gmail through read-only scopes.

## Limits and next steps

- Send, book and pay are simulated stubs behind the real gate. A real integration replaces one function.
- Live Google and Places behavior requires the owner's own credentials and was tested against mocked responses.
- Next: real channel integrations, push delivery of the briefing, a voice client, and learning preferences from approvals.
