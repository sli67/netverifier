-- What the user wants changed, in their own words. Null until they describe it.
alter table investigations
  add column intent            text,
  add column intent_updated_at timestamptz,
  add column intent_updated_by uuid references users (id);
