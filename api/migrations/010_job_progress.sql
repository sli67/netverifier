-- What a running job is doing right now, for the dashboard to show live:
-- { "step": <tool calls finished>, "doing": "<what is happening>" }.
-- Written by the worker as it goes; meaningful only while the job runs.
alter table jobs add column progress jsonb;
