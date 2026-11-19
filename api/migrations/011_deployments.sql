-- Deploying an approved candidate to a Containerlab lab, and verifying it there.
--
-- A deployment boots the lab on the snapshot's current configs, records the
-- live state, boots the changed devices on the approved candidate, and checks
-- the live state against what Batfish predicted for the candidate. On a
-- mismatch it boots the current configs again.
create table deployments (
  id             uuid primary key default gen_random_uuid(),
  run_id         uuid not null references runs (id),
  snapshot_id    uuid not null references snapshots (id),
  candidate_id   uuid not null references candidates (id),
  -- The approved content. The worker re-checks the candidate's files against it.
  candidate_hash text not null,
  status         text not null default 'queued'
                 check (status in ('queued', 'deploying', 'verifying', 'completed', 'rolled_back', 'failed')),
  -- The step under way or last finished: baseline, candidate, verify, rollback.
  stage          text,
  lab_name       text,
  -- [{ "at": ..., "step": ..., "message": ... }], appended as the deployment goes.
  log            jsonb not null default '[]',
  -- Live state before and after, Batfish's prediction, and the comparison.
  result         jsonb,
  error          text,
  created_by     uuid not null references users (id),
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  finished_at    timestamptz
);

create index deployments_run_idx on deployments (run_id, created_at desc);

-- One lab per snapshot, so only one deployment may be changing it at a time.
create unique index deployments_one_active_idx
  on deployments (snapshot_id)
  where status in ('queued', 'deploying', 'verifying');

alter table jobs add column deployment_id uuid references deployments (id);
alter table jobs drop constraint jobs_kind_check;
alter table jobs add constraint jobs_kind_check
  check (kind in ('parse_snapshot', 'agent_reply', 'run_workflow', 'deploy_change'));
