# Front-door readiness (design note, no code)

This note shows how the assistant's actions would map to the built-in assistants on iPhone and Android.
Nothing here is built. The backend is already shaped for it: every capability is a small authenticated
HTTP call, and every write goes through the approval gate. A future phone app would be a thin client
over these endpoints.

Platform details were checked against the official documentation on 31 October 2026. Both platforms
change quickly, so check the links at the end before building.

## Principle

An assistant may start work and read results. Only the user, on the approval screen, approves. A spoken
"yes", or an agent saying the user agreed, is never enough to send, book or pay.

## Intent map

| Capability | Type | Backend call | Behavior on any front door |
|---|---|---|---|
| Morning briefing | Read | `POST /api/briefing/run`, then `GET /api/briefing/latest` | Reads the headline and top priorities aloud. |
| Plan a dinner (friend, day, cuisine) | Proposes | `POST /api/chat` with the parsed request | Says what it found, then opens the approval screen. Nothing is sent or booked. |
| Pending approvals | Read | `GET /api/approvals?status=pending` | Reads the count and one-line summaries. |
| Approve an action | Write | `POST /api/approvals/{id}/approve` | Only inside the app, after device authentication, showing the exact message or booking. See the platform notes. |
| Reject an action | Write-safe | `POST /api/approvals/{id}/reject` | Safe by voice. Rejecting never causes a side effect. |
| Choose another restaurant | Proposes | `POST /api/dinner/{plan_id}/choose` | Re-proposes with the new choice. Still needs approval. |

## iPhone: Apple App Intents (Siri, Shortcuts, Apple Intelligence)

App Intents (iOS 16 and later) is the framework that makes an app's actions available to Siri, the
Shortcuts app and other system features.

| Capability | App Intent | Notes |
|---|---|---|
| Morning briefing | `MorningBriefingIntent` | Read. May run without opening the app. |
| Plan a dinner | `PlanDinnerIntent` (friend, day, cuisine as parameters) | Ends by continuing in the app (`continueInForeground`) so the person sees the approval screen. |
| Pending approvals | `PendingApprovalsIntent` | Read. |
| Approve | `ApproveActionIntent` (action as parameter) | Shows a confirmation with the full payload (`requestConfirmation`) and sets an authentication policy (`IntentAuthenticationPolicy`) so the device must be unlocked and authenticated. |
| Reject | `RejectActionIntent` | Safe by voice. |
| Choose restaurant | `ChooseRestaurantIntent` | Proposes only. |

Rules:
1. Read intents may run without opening the app.
2. Proposing intents may run, but must end by showing the approval screen.
3. Approve uses the confirmation prompt with the full payload on screen, plus an authentication policy.
4. Entities expose the summary and payload for display, never the hash or internal ids.
5. Send the access token from the keychain. Never embed it in a Shortcut.

## Android: two routes, with different maturity

**Route 1: App Actions (Google Assistant).** The app declares built-in intents as capabilities in
`shortcuts.xml`, and Assistant fulfils them with an Android intent or a deep link. The app must be on
Google Play, and App Actions are reviewed before reaching users. This fits our read and propose flows:

| Capability | Fit | Notes |
|---|---|---|
| Morning briefing | Open a feature | Deep link to the briefing screen. |
| Plan a dinner | Open a feature or a custom intent | Deep link into the chat with the request prefilled. |
| Pending approvals | Open a feature | Deep link to the approval screen. |

**Route 2: AppFunctions (agents such as Gemini).** AppFunctions lets an app expose functions that AI
agents can discover and call on the device (Android 16 and later), declared with `@AppFunction`. Callers
need the `EXECUTE_APP_FUNCTIONS` permission. At the time of writing the API is experimental, and Gemini's
access to it is in a private preview, so this route is a plan, not something to promise.

| Capability | AppFunction | Notes |
|---|---|---|
| Morning briefing | `getBriefing` | Read. |
| Plan a dinner | `proposeDinner` | Proposes. Returns the plan and the approval link. |
| Pending approvals | `listPendingApprovals` | Read. |
| Reject | `rejectAction` | Safe. |
| Approve | **Not exposed** | The documentation does not describe a user-confirmation step for agent calls, so approval stays on the approval screen inside the app. |

Rule for both Android routes: the agent never approves. It reads, proposes, or opens the approval screen.

## Why this is cheap to add later

- The API is stateless JSON with stable error codes (404, 409, 410, 422, 429).
- Approvals are idempotent, so a flaky mobile connection that retries cannot double-send.
- The audit log records the actor for each approval, so a phone approval can be told apart from a web one.
- The gate does not care who asks. A voice client, an agent and a browser all go through the same checks.

## Sources checked

- Apple: [AppIntent](https://developer.apple.com/documentation/appintents/appintent)
- Android: [App Actions overview and setup](https://developer.android.com/develop/devices/assistant/get-started)
- Android: [AppFunctions overview](https://developer.android.com/ai/appfunctions)
