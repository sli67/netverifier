-- The conversation between users and the agent about one investigation.
create table messages (
  id               uuid primary key default gen_random_uuid(),
  investigation_id uuid not null references investigations (id),
  role             text not null check (role in ('user', 'agent')),
  content          text not null,
  -- Agent messages only: the tool calls made while producing the reply,
  -- [{ "tool": "...", "args": {...}, "summary": "..." }, ...].
  steps            jsonb,
  -- True for an agent message that reports it could not produce a reply.
  failed           boolean not null default false,
  -- Null for agent messages.
  created_by       uuid references users (id),
  created_at       timestamptz not null default now()
);

create index messages_investigation_idx on messages (investigation_id, created_at);

-- A user message queues an agent_reply job for the worker.
alter table jobs drop constraint jobs_kind_check;
alter table jobs add constraint jobs_kind_check check (kind in ('parse_snapshot', 'agent_reply'));
