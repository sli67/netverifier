-- A run is one pass of the staged workflow over an investigation's intent:
-- clarify, investigate, baseline checks, and for a change: propose, validate,
-- report and approve.
create table runs (
  id               uuid primary key default gen_random_uuid(),
  investigation_id uuid not null references investigations (id),
  -- 'verify' stops after checking the current configs; 'change' goes on to propose a fix.
  mode             text not null check (mode in ('verify', 'change')),
  -- The intent as it was when the run started. Later edits do not affect the run.
  intent           text not null,
  status           text not null default 'queued'
                   check (status in ('queued', 'running', 'waiting_user', 'waiting_approval',
                                     'done', 'failed', 'cancelled')),
  -- The step the run is in or last finished.
  stage            text,
  -- How the run ended: holds, violated, inconclusive, no_change_needed,
  -- not_validated, approved or rejected. Null until then.
  outcome          text,
  -- What the dashboard shows: requirements, checks, results, candidate summary.
  -- The worker's own working state is in the LangGraph checkpoint tables.
  state            jsonb not null default '{}',
  error            text,
  created_by       uuid not null references users (id),
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  finished_at      timestamptz
);

create index runs_investigation_idx on runs (investigation_id, created_at desc);

-- At most one run per investigation may be under way or waiting for a person.
create unique index runs_one_active_idx
  on runs (investigation_id)
  where status in ('queued', 'running', 'waiting_user', 'waiting_approval');

-- A candidate is one proposed set of configs. Unchanged files stay in the
-- snapshot; changed files are stored under s3_prefix.
create table candidates (
  id            uuid primary key default gen_random_uuid(),
  run_id        uuid not null references runs (id),
  -- 1 for the first proposal, 2 and 3 for repairs after failed validation.
  round         integer not null,
  -- Computed the same way as a snapshot's content hash, over every file.
  content_hash  text not null,
  s3_prefix     text not null,
  -- [{ "path": "...", "sha256": "...", "size": 123, "changed": true }, ...]
  files         jsonb not null,
  diff          text not null,
  summary       text not null,
  created_at    timestamptz not null default now(),
  unique (run_id, round)
);

-- A decision is bound to the exact candidate content that was reviewed.
create table approvals (
  id              uuid primary key default gen_random_uuid(),
  run_id          uuid not null references runs (id),
  candidate_id    uuid not null references candidates (id),
  candidate_hash  text not null,
  decision        text not null check (decision in ('approved', 'rejected')),
  comment         text,
  decided_by      uuid not null references users (id),
  decided_at      timestamptz not null default now(),
  unique (run_id)
);

-- A run_workflow job starts or resumes a run. payload says which:
-- { "action": "start" }, { "action": "answer", "answer": "..." } or
-- { "action": "decision", "decision": "approved", ... }.
alter table jobs add column run_id uuid references runs (id);
alter table jobs add column payload jsonb;
alter table jobs drop constraint jobs_kind_check;
alter table jobs add constraint jobs_kind_check check (kind in ('parse_snapshot', 'agent_reply', 'run_workflow'));
