# Siri readiness (design note, no code)

This note shows how the assistant's actions would map to Apple App Intents. Nothing here is built.
The backend is already shaped for it: every capability is a small authenticated HTTP call, and every
write goes through the approval gate. A future iOS app would be a thin client over these endpoints.

## Principle

Siri can start and read. Only the user, inside the app, approves. A spoken "yes" is never enough to
send, book or pay on its own.

## Intent map

| App Intent | Type | Backend call | Siri behavior |
|---|---|---|---|
| `MorningBriefingIntent` | Read | `POST /api/briefing/run`, then `GET /api/briefing/latest` | Reads the headline and top priorities aloud. |
| `PlanDinnerIntent` (parameters: friend, day, cuisine) | Proposes | `POST /api/chat` with the parsed request | Says what it found, then opens the approval screen. Nothing is sent or booked. |
| `PendingApprovalsIntent` | Read | `GET /api/approvals?status=pending` | Reads the count and one-line summaries. |
| `ApproveActionIntent` (parameter: action) | Write | `POST /api/approvals/{id}/approve` | Requires an authenticated confirmation on device (Face ID or passcode) and a confirmation dialog that shows the exact message or booking. |
| `RejectActionIntent` (parameter: action) | Write-safe | `POST /api/approvals/{id}/reject` | Safe to do by voice. Rejecting never causes a side effect. |
| `ChooseRestaurantIntent` | Proposes | `POST /api/dinner/{plan_id}/choose` | Re-proposes with the new choice. Still needs approval. |

## Rules for any voice or Shortcuts client

1. **Read intents** may run without opening the app.
2. **Proposing intents** may run, but must end by showing the approval screen.
3. **Approve** must use `requestConfirmation` with the full payload on screen and require device authentication.
4. **Reject** may run by voice.
5. Entities (`ActionEntity`) expose the summary and payload for display, never the hash or internal ids.
6. Send the access token from the keychain. Never embed it in a Shortcut.

## Why this is cheap to add later

- The API is already stateless JSON with stable error codes (404, 409, 410, 422, 429).
- Approvals are idempotent, so a flaky mobile connection that retries cannot double-send.
- The audit log records the actor for each approval, so a phone approval can be told apart from a web one.
