create table users (
  id            uuid primary key default gen_random_uuid(),
  -- Stored lowercased by the application.
  email         text not null unique,
  password_hash text not null,
  role          text not null check (role in ('requester', 'approver', 'admin')),
  disabled_at   timestamptz,
  created_at    timestamptz not null default now()
);

create table sessions (
  id         uuid primary key default gen_random_uuid(),
  -- SHA-256 of the cookie token, so a database leak does not yield usable sessions.
  token_hash bytea not null unique,
  user_id    uuid not null references users (id) on delete cascade,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null
);

create index sessions_user_id_idx on sessions (user_id);
