-- Executive Assistant Agent: Supabase / PostgreSQL schema.
-- Idempotent: safe to run more than once.
-- Row Level Security is enabled with no policies, so the public Supabase API
-- (anon / authenticated keys) can read nothing. The backend connects with the
-- database role (DATABASE_URL), which bypasses RLS.

create table if not exists app_users (
  user_id     text primary key,
  created_at  timestamptz not null default now(),
  last_seen   timestamptz not null default now()
);

create table if not exists preferences (
  user_id     text primary key,
  data        jsonb not null default '{}'::jsonb,
  updated_at  timestamptz not null default now()
);

create table if not exists contacts (
  user_id     text not null,
  name        text not null,
  handle      text,
  notes       text,
  primary key (user_id, name)
);

-- Tokens are encrypted by the application (Fernet) before they reach this table.
create table if not exists oauth_tokens (
  user_id     text not null,
  provider    text not null,
  ciphertext  text not null,
  scopes      text,
  updated_at  timestamptz not null default now(),
  primary key (user_id, provider)
);

create table if not exists plans (
  plan_id     text primary key,
  user_id     text not null,
  kind        text not null check (kind in ('briefing','dinner')),
  data        jsonb not null,
  created_at  timestamptz not null default now()
);
create index if not exists plans_user_kind_idx on plans (user_id, kind, created_at desc);

create table if not exists messages (
  id          bigserial primary key,
  user_id     text not null,
  role        text not null check (role in ('user','assistant')),
  text        text not null,
  meta        jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default now()
);
create index if not exists messages_user_idx on messages (user_id, id);

create table if not exists actions (
  action_id        text primary key,
  user_id          text not null,
  kind             text not null check (kind in ('send_message','book_table','pay')),
  payload          jsonb not null,
  payload_hash     text not null,
  summary          text not null,
  status           text not null check (status in
                   ('pending','executed','rejected','expired','failed','superseded')),
  flow_id          text,
  idempotency_key  text not null,
  created_at       timestamptz not null default now(),
  expires_at       timestamptz not null,
  decided_at       timestamptz,
  decided_by       text,
  result           jsonb
);
create index if not exists actions_user_status_idx on actions (user_id, status, created_at desc);
-- One live proposal per idempotency key. Rejected / expired ones can be re-proposed.
create unique index if not exists actions_live_key_idx
  on actions (user_id, idempotency_key) where status in ('pending','executed');

-- A recorded approval. UNIQUE(action_id) makes approvals idempotent at the database level.
create table if not exists approvals (
  approval_id   text primary key,
  action_id     text not null unique references actions(action_id) on delete cascade,
  user_id       text not null,
  payload_hash  text not null,
  approved_by   text not null,
  approved_at   timestamptz not null default now()
);

-- Simulated side effects. Real senders / bookers replace the stubs that write here.
create table if not exists outbox (
  outbox_id    text primary key,
  action_id    text not null unique references actions(action_id) on delete cascade,
  user_id      text not null,
  kind         text not null,
  payload      jsonb not null,
  executed_at  timestamptz not null default now()
);

-- Append-only, hash-chained audit log (one chain per user).
create table if not exists audit_log (
  seq        bigserial primary key,
  user_id    text not null,
  ts         timestamptz not null default now(),
  event      text not null,
  action_id  text,
  actor      text not null,
  details    jsonb not null default '{}'::jsonb,
  prev_hash  text not null,
  hash       text not null
);
create index if not exists audit_user_idx on audit_log (user_id, seq);

do $$
declare t text;
begin
  foreach t in array array['app_users','preferences','contacts','oauth_tokens','plans',
                           'messages','actions','approvals','outbox','audit_log']
  loop
    execute format('alter table %I enable row level security', t);
  end loop;
end $$;
