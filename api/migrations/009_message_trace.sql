-- What ran for each agent message, so a conversation can be audited.

-- How the user's message was sorted after the agent replied. Null when it
-- could not be sorted, and on messages from before this was recorded.
alter table messages add column route text check (route in ('question', 'check', 'change'));
-- Time the worker spent producing the reply, and the model that wrote it.
alter table messages add column duration_ms integer;
alter table messages add column model text;

-- The agent message whose suggestion started this run. Null for runs started
-- from the intent card.
alter table runs add column message_id uuid references messages (id) on delete set null;
create index runs_message_idx on runs (message_id) where message_id is not null;
