-- An investigation is the user-facing record of analysing one snapshot.
create table investigations (
  id               uuid primary key default gen_random_uuid(),
  snapshot_id      uuid not null references snapshots (id),
  status           text not null default 'queued'
                   check (status in ('queued', 'parsing', 'ready', 'failed')),
  created_by       uuid not null references users (id),
  created_at       timestamptz not null default now(),
  started_at       timestamptz,
  finished_at      timestamptz,
  -- True when Batfish already held a parsed model for this content.
  model_reused     boolean,
  -- Which Batfish instance holds the parsed model, and its version.
  batfish_instance text,
  batfish_version  text,
  parse_ms         integer,
  -- { "files": [...], "issues": [...] } as reported by Batfish.
  result           jsonb,
  error            text
);

create index investigations_snapshot_idx on investigations (snapshot_id, created_at desc);

-- At most one investigation per snapshot may be waiting or running.
create unique index investigations_one_active_idx
  on investigations (snapshot_id)
  where status in ('queued', 'parsing');

-- A job is the unit of work a worker claims. Workers hold a job through an
-- expiring lease, and every write a worker makes is conditional on its
-- attempt_token, so a worker that lost its lease cannot commit late results.
create table jobs (
  id               uuid primary key default gen_random_uuid(),
  kind             text not null check (kind in ('parse_snapshot')),
  investigation_id uuid not null references investigations (id),
  status           text not null default 'queued'
                   check (status in ('queued', 'running', 'done', 'failed')),
  attempts         integer not null default 0,
  max_attempts     integer not null default 3,
  run_after        timestamptz not null default now(),
  attempt_token    uuid,
  locked_by        text,
  lease_expires_at timestamptz,
  last_error       text,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);

create index jobs_claim_idx on jobs (status, run_after);
