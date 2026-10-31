# Connect your own Google Calendar and Gmail (read-only)

Only needed for `DATA_MODE=google` in private mode. The public demo never uses this.

## 1. Create the OAuth client

1. Open the Google Cloud console and create a project (or pick one).
2. **APIs and services > Library**: enable **Google Calendar API** and **Gmail API**.
3. **Google Auth Platform > Branding / Audience**: set the app to **External**, keep it in **Testing**, and add your own Google address under **Test users**.
4. **Data Access**: add exactly these two scopes and nothing else:
   - `https://www.googleapis.com/auth/calendar.readonly`
   - `https://www.googleapis.com/auth/gmail.readonly`
5. **Clients > Create client > Web application**. Add the redirect URI:
   - Local: `http://localhost:8000/auth/google/callback`
   - Deployed: `https://<your-domain>/auth/google/callback`
6. Copy the client id and secret.

Testing mode is enough for a personal assistant on your own account. Refresh tokens issued to a
Testing app expire after 7 days, so you reconnect weekly. Publishing the app for production use of
sensitive scopes requires Google's verification, which you only need if other people will connect their accounts.

## 2. Configure the app

```
ACCESS_TOKEN=<long random string>
DATA_MODE=google
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REDIRECT_URI=<the exact URI from step 5>
TOKEN_ENCRYPTION_KEY=<python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
```

## 3. Connect

1. Start the app and sign in with your `ACCESS_TOKEN`.
2. Open **Memory** and click **Connect Google (read-only)**.
3. Approve the consent screen. You return to the Memory page showing **Connected**.
4. On the chat page run **Morning briefing**. The Audit log shows `google_connected` and each `tool_read`.

## What the app asks Google for

- Calendar: events in the next two days, titles, times, location and attendee names.
- Gmail: up to 15 inbox messages from the last two days, fetched with `format=metadata`: sender, subject, date, snippet and the unread label. Bodies and attachments are never requested.
- A token that comes back with any additional scope is refused and not stored.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `redirect_uri_mismatch` | The redirect URI in the console must match `GOOGLE_REDIRECT_URI` exactly, including scheme and path. |
| "Google did not return a refresh token" | Revoke the app at myaccount.google.com/permissions and connect again. |
| Briefing says Google is not connected | The token was deleted or the encryption key changed. Reconnect on the Memory page. |
| Access blocked: app not verified | Add your address under Test users, or use a Testing-mode app. |
| Works for a week, then stops | Testing-mode tokens expire after 7 days. Reconnect. |
