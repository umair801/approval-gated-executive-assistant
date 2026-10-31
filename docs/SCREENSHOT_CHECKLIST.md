# Screenshot checklist

Take these from the **deployed** app in sample-data mode (or locally with `uvicorn`), in a private
window. Browser width 1440, zoom 100 percent. Crop out the address bar unless you have confirmed the
URL works and want it visible. Never capture real calendar, mail, tokens or `.env` files.

Draft versions of 57a to 57e, rendered from the local sample data, are in `docs/screenshots_draft/`
so you can see the target composition. Retake them from the live deployment.

| File | Page | Prepare | Must be visible |
|---|---|---|---|
| `57a_assistant_home.png` | `/` | Run one briefing and one dinner plan so the thread has both replies | Header with nav and the Approvals count badge, quick-action buttons, the chat thread, "Sample data" and "Mock model" or "Claude" chips |
| `57b_assistant_morning_briefing.png` | `/plan` | Click **Run briefing now** | Headline, conflicts (an overlap and a back-to-back), priorities with the VIP mail first, the schedule with conflict badges, and the red "instructions aimed at an AI assistant" notice |
| `57c_assistant_dinner_plan.png` | `/dinner/<plan>` (open from the chat link) | Plan dinner with Sam on Friday | Free slots with the best one marked, three restaurant cards with one **Selected**, the draft message, "Not sent" note |
| `57d_assistant_approval_queue.png` | `/approvals` | Leave both proposals pending | Both cards: message with editable text, booking details, **Approve** and **Reject** buttons, expiry time |
| `57e_assistant_audit_log.png` | `/audit` | Approve one action first | **Hash chain verified** badge, events `action_proposed`, `approval_recorded`, `action_executed`, and `suspicious_content_ignored` |
| `57f_assistant_api_docs.png` | `/docs` | Expand the **approvals** group | Endpoint list showing approve and reject, and the title "Executive Assistant Agent" |
| `57g_assistant_github.png` | Your GitHub repo | After the push | README top with the architecture diagram rendered, file tree, tests badge or CI check |

## Before you publish any of them

- No real names, emails, calendar entries or tokens.
- No URL you have not opened and confirmed.
- File names exactly as in the table, lowercase, `.png`.
