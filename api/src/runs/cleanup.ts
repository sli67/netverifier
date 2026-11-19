import type { PoolClient } from "pg";

/**
 * Deletes the runs of the investigations selected by `investigations` (a
 * subquery returning investigation ids, using $1), with their candidates,
 * approvals and workflow checkpoints. Jobs must already be deleted, since they
 * reference runs.
 *
 * Returns the object-storage keys of the candidates' files, for the caller to
 * remove once the transaction has committed.
 */
export async function deleteRuns(client: PoolClient, investigations: string, param: string): Promise<string[]> {
  const runs = `select id from runs where investigation_id in (${investigations})`;

  const candidates = await client.query<{ s3_prefix: string; files: { path: string; changed: boolean }[] }>(
    `select s3_prefix, files from candidates where run_id in (${runs})`,
    [param],
  );
  const keys = candidates.rows.flatMap((c) =>
    c.files.filter((f) => f.changed).map((f) => `${c.s3_prefix}configs/${f.path}`),
  );

  // The worker creates LangGraph's checkpoint tables the first time it starts,
  // so they may not exist yet.
  const tables = await client.query<{ name: string }>(
    `select t.name from unnest(array['checkpoint_writes', 'checkpoint_blobs', 'checkpoints']) as t(name)
      where to_regclass('public.' || t.name) is not null`,
  );
  for (const { name } of tables.rows) {
    await client.query(`delete from ${name} where thread_id in (select id::text from (${runs}) r)`, [param]);
  }

  await client.query(`delete from deployments where run_id in (${runs})`, [param]);
  await client.query(`delete from approvals where run_id in (${runs})`, [param]);
  await client.query(`delete from candidates where run_id in (${runs})`, [param]);
  await client.query(`delete from runs where investigation_id in (${investigations})`, [param]);
  return keys;
}
