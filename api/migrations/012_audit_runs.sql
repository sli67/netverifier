-- Audit runs: infer what a named route-map, policy, list or ACL is meant to do,
-- confirm that with a person, and check it on Batfish.
alter table runs drop constraint runs_mode_check;
alter table runs add constraint runs_mode_check check (mode in ('verify', 'change', 'audit'));
-- The name of the structure an audit run examines. Null for other runs.
alter table runs add column target text;
alter table runs add constraint runs_target_check check ((mode = 'audit') = (target is not null));
