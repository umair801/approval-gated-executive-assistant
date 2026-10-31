# Deploy: Supabase + Railway + your subdomain

Order matters: database first, then the service, then the domain.

## 1. Supabase (database)

1. Create a Supabase project. Pick the region closest to your Railway region.
2. Open **Connect** and copy the **Session pooler** connection string (port 5432). It looks like
   `postgresql://postgres.<ref>:<password>@<region>.pooler.supabase.com:5432/postgres`.
3. You do **not** need to run `schema.sql` by hand. The app applies it on start (it is idempotent).
   If you prefer, paste `schema.sql` into the SQL editor once.
4. Row Level Security is already enabled by the schema. Do not add public policies.

## 2. Push the code

Push this folder to a **new GitHub repository**. `internal/` and `.env` are git-ignored. Confirm
with `git status` that no `.env` file is staged before you push.

## 3. Railway service

1. **New project > Deploy from GitHub repo**. Railway detects the `Dockerfile` and `railway.json`.
2. Add these **Variables** (generate the secrets locally):

| Variable | Value |
|---|---|
| `APP_ENV` | `production` |
| `ACCESS_TOKEN` | long random string, for private mode. **Or** leave empty and set `ALLOW_PUBLIC_DEMO=true` for a public sample-data demo. |
| `DATABASE_URL` | the Supabase session pooler string |
| `TABLE_PREFIX` | your subdomain, for example `assistant`. Every table and index is created as `assistant_plans`, `assistant_audit_log` and so on. Leave empty for a database used by this project alone. |
| `TOKEN_ENCRYPTION_KEY` | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `ANTHROPIC_API_KEY` | your key (optional, the mock model is used without it) |
| `CLAUDE_MODEL` | `claude-sonnet-5-5` or the model you choose |
| `DAILY_LLM_CALL_CAP` | for a public demo, keep it low, for example `150` |
| `TIMEZONE` | for example `Asia/Shanghai` |
| `BRIEFING_ENABLED` | `true` to send the morning briefing automatically (private mode) |
| `BRIEFING_TIME` | for example `07:30` |
| `CRON_TOKEN` | only if you use the external cron option below |

3. Deploy. The healthcheck is `GET /health`. It returns 503 if the database is unreachable.

## 4. Subdomain

1. Railway: **Settings > Networking > Custom domain**, enter `<name>.datawebify.com`. Railway shows a CNAME target.
2. In your DNS, add a **CNAME** record `<name>` pointing to that target. Wait for the certificate to issue.
3. Update `GOOGLE_REDIRECT_URI` and the Google console if you use Google mode.

## 5. Scheduled briefing (pick one)

**A. In the app (simplest).** `BRIEFING_ENABLED=true`. A background loop runs the briefing once per local day after `BRIEFING_TIME`.

**B. External cron.** Create a second Railway service from the same repo with a cron schedule and this start command:

```
python -c "import os,urllib.request as u;r=u.Request(os.environ['APP_URL']+'/api/jobs/briefing',method='POST',headers={'Authorization':'Bearer '+os.environ['CRON_TOKEN']});print(u.urlopen(r,timeout=60).read())"
```

Set `APP_URL` and `CRON_TOKEN` on that service. The endpoint is idempotent, so a repeated call the same day does nothing.

## 6. Verify

```bash
python scripts/readiness_check.py --url https://<name>.datawebify.com --token <ACCESS_TOKEN>
```

Then run one briefing and one dinner plan in the UI, approve one action, and open the Audit log. It should
show **Hash chain verified**.

## Cost control for a public demo

- Leave `ANTHROPIC_API_KEY` empty to run the mock model for free, or set a low `DAILY_LLM_CALL_CAP`.
- Per-client rate limiting is on by default (`RATE_LIMIT_PER_MIN`).
- Demo sandboxes are deleted after 24 hours.
