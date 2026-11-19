import { ensureDatabase, runMigrations } from "../src/migrations.js";
import { TEST_DATABASE_URL } from "./env.js";

export default async function setup(): Promise<void> {
  await ensureDatabase(TEST_DATABASE_URL);
  await runMigrations(TEST_DATABASE_URL);
}
