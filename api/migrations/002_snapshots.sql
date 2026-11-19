-- A snapshot is an immutable set of device configurations. The file contents
-- live in object storage under s3_prefix; this row is the index to them.
create table snapshots (
  id           uuid primary key,
  name         text not null,
  s3_prefix    text not null unique,
  -- SHA-256 over the sorted (file hash, path) manifest: identifies exact content.
  content_hash text not null,
  -- [{ "path": "...", "sha256": "...", "size": 123 }, ...] sorted by path.
  files        jsonb not null,
  file_count   integer not null,
  total_bytes  integer not null,
  created_by   uuid not null references users (id),
  created_at   timestamptz not null default now()
);

create index snapshots_created_at_idx on snapshots (created_at desc);
