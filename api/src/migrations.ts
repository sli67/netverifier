import { readdir, readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import pg from "pg";

const dir = fileURLToPath(new URL("../migrations/", import.meta.url));

/** Creates the database named in the URL if it does not exist yet. */
export async function ensureDatabase(databaseUrl: string): Promise<void> {
  const url = new URL(databaseUrl);
  const name = decodeURIComponent(url.pathname.slice(1));
  if (!/^[A-Za-z0-9_]+$/.test(name)) throw new Error(`unsupported database name: ${name}`);
  url.pathname = "/postgres";

  const admin = new pg.Client({ connectionString: url.toString() });
  await admin.connect();
  try {
    const exists = await admin.query("select 1 from pg_database where datname = $1", [name]);
    if (exists.rowCount === 0) await admin.query(`create database "${name}"`);
  } finally {
    await admin.end();
  }
}

/** Applies every migration file that has not been applied yet, in name order. */
export async function runMigrations(databaseUrl: string, log: (line: string) => void = () => {}): Promise<void> {
  const client = new pg.Client({ connectionString: databaseUrl });
  await client.connect();
  try {
    // Serialises concurrent runs, such as two test processes starting together.
    await client.query("select pg_advisory_lock(7241001)");
    await client.query(
      "create table if not exists schema_migrations (name text primary key, applied_at timestamptz not null default now())",
    );
    const applied = new Set(
      (await client.query<{ name: string }>("select name from schema_migrations")).rows.map((r) => r.name),
    );
    const files = (await readdir(dir)).filter((f) => f.endsWith(".sql")).sort();

    for (const file of files) {
      if (applied.has(file)) continue;
      const sql = await readFile(dir + file, "utf8");
      await client.query("begin");
      try {
        await client.query(sql);
        await client.query("insert into schema_migrations (name) values ($1)", [file]);
        await client.query("commit");
        log(`applied ${file}`);
      } catch (err) {
        await client.query("rollback");
        throw err;
      }
    }
  } finally {
    await client.end();
  }
}

/** The private database tests run against: the configured one with a _test suffix. */
export function testDatabaseUrl(databaseUrl: string): string {
  const url = new URL(databaseUrl);
  url.pathname = `${url.pathname}_test`;
  return url.toString();
}
