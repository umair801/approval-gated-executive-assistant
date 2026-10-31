# Data and privacy

## Principle

Store the minimum needed to do the job, keep it only as long as it is useful, and make it easy to
see and delete.

## What is stored

| Data | Purpose | Contains | Retention |
|---|---|---|---|
| Preferences | Rank restaurants and mail | Home area, favorite cuisines, price ceiling, VIP sender names, last few cuisines planned | Until you edit or delete |
| Contacts | Address messages | Name, handle you typed, notes you typed | Until you edit or delete |
| Messages | Show the chat | Your requests and the assistant's replies | Until deleted |
| Plans | Show today's plan and dinner options | The briefing result and the dinner shortlist. For mail: only sender and subject of the few items that made the priority list. | Until deleted |
| Actions and approvals | The approval queue | Proposed message text, booking details, who approved and when | Until deleted |
| Outbox | Record of simulated sends and bookings | Same as the approved action | Until deleted |
| Audit log | Prove what happened | Event type, actor, ids, counts. No message text, no email content. | Until deleted |
| Google refresh token | Read calendar and mail | Encrypted (Fernet). Never logged. | Until you disconnect or delete |

## What is never stored

- Email bodies and attachments (Gmail is read with `format=metadata`)
- Calendar event descriptions
- Google access tokens (they live in process memory for about an hour)
- Passwords, payment details, government ids

## Your rights

| Right | How |
|---|---|
| Access and portability | `GET /api/me/data` or the **Download my data** button on the Privacy page. Returns JSON. |
| Erasure | `DELETE /api/me/data` or the **Delete all my data** button. Removes every row listed above for your user, including the audit log. |
| Rectification | Edit preferences and contacts on the Memory page. |
| Transparency | The Privacy page shows what is stored with counts. The audit log shows every read. |

## After deletion

A single tombstone entry remains on a system chain: the event name, a **hashed** user id (first 16 hex
characters of SHA-256) and row counts. It contains no personal data and lets an operator prove a deletion happened.

## Public demo

Each visitor gets a random sandbox id in a cookie, with fictional data only. Sandboxes are deleted
after 24 hours of inactivity. The demo never connects to a real account.

## Operator checklist

- Keep `TOKEN_ENCRYPTION_KEY` in the platform's secret store. Rotating it invalidates stored tokens, and users reconnect.
- Choose a Supabase region that fits your users' data location requirements.
- Row Level Security is enabled on every table with no public policies. Connect with the database role, not the public API key.
- Add a privacy notice and a data processing agreement if you offer this to customers.
