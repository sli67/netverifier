-- Chat messages can be sorted as a request for an audit of a named structure.
alter table messages drop constraint messages_route_check;
alter table messages add constraint messages_route_check check (route in ('question', 'check', 'change', 'audit'));
