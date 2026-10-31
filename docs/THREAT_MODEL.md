# Threat model

What could go wrong, what stops it, and which test proves it. Run `python -m pytest` to re-check all of these.

| # | Threat | Defense | Proof |
|---|---|---|---|
| 1 | The model or a bug tries to send, book or pay directly | The tool registry has no executable write tools. Direct calls raise and are audited. | `test_write_tools_cannot_be_called_directly` |
| 2 | A side effect runs without approval | The executor re-reads the recorded approval from the database and refuses without it. | `test_executor_refuses_without_recorded_approval` |
| 3 | Payload changed after the user saw it | The approval stores a SHA-256 of the payload. The executor and the gate re-check it. | `test_executor_refuses_when_payload_changed_after_approval`, `test_tampered_stored_payload_blocks_approval` |
| 4 | Double click, retry or race runs the action twice | `UNIQUE(action_id)` on approvals and on the outbox. Status moves by compare-and-set. | `test_double_approve_is_idempotent`, `test_concurrent_approvals_execute_once` |
| 5 | An old suggestion is approved days later | Proposals expire (24 hours by default). | `test_expired_proposal_cannot_be_approved` |
| 6 | An email tells the assistant to do something | Mail is data. Instruction-like mail is flagged, excluded and reported. The briefing flow has no write path at all. | `test_prompt_injection_email_is_ignored_and_flagged` |
| 7 | A restaurant listing or model draft smuggles in a link, address or number | Drafts containing URLs, `@` or long numbers are replaced by a safe template. | `test_unsafe_draft_with_link_is_replaced_by_template` |
| 8 | The model invents conflicts or priorities | Conflicts and focus windows are computed in code and overwrite the model's. Unknown references are dropped. | `test_briefing_drops_priorities_and_conflicts_the_model_invented` |
| 9 | Someone edits the audit log after the fact | Entries are hash-chained per user. The UI and API verify the chain. | `test_audit_chain_detects_tampering`, `test_audit_chain_detects_deleted_entry` |
| 10 | A stolen database dump leaks Google access | Refresh tokens are encrypted (Fernet). Row Level Security is on with no public policies. | `test_refresh_token_is_encrypted_at_rest` |
| 11 | Over-broad Google access | Only read-only scopes are requested. A token with extra scopes is refused and not stored. | `test_token_with_broader_scope_is_refused_and_not_stored` |
| 12 | A visitor reads another visitor's data (public demo) | One sandbox per visitor, forged ids are replaced, sandboxes are purged after 24 hours. | `test_demo_visitors_are_isolated_from_each_other`, `test_forged_demo_cookie_is_replaced`, `test_stale_demo_sandboxes_are_purged` |
| 13 | The public demo touches a real account | Demo mode forces sample data and disables the Google routes. | `test_demo_mode_never_exposes_google_or_cron` |
| 14 | Cross-site request forgery | Origin check on every state-changing request, plus `HttpOnly` `SameSite` cookies. | `test_cross_origin_post_is_refused` |
| 15 | Brute force or request flooding | Per-client rate limits and a stricter limit on login. | `test_rate_limit_returns_429_with_retry_after`, `test_login_attempts_are_rate_limited` |
| 16 | Cross-site scripting | Templates auto-escape. A strict Content-Security-Policy blocks inline scripts. | `test_user_text_is_html_escaped`, `test_security_headers_present` |
| 17 | Cost abuse of the model key | A daily call cap and per-client rate limits. | `test_daily_cap_blocks_further_calls` |
| 18 | Misconfigured production | The app refuses to start without access control and an encryption key. | `test_production_refuses_open_access_and_missing_key` |

## Not covered

- A compromised host or database administrator can read stored data. Use Supabase access controls and rotate keys.
- The stubs do not move money or send mail, so there is no real-world side effect to protect yet. When a real
  integration is added, add an idempotency key to the provider call as well.
