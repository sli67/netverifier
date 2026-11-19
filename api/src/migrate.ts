// Usage: migrate [--test]
// --test creates and migrates the private test database instead.
import { ensureDatabase, runMigrations, testDatabaseUrl } from "./migrations.js";

const configured = process.env.DATABASE_URL;
if (!configured) throw new Error("DATABASE_URL is not set");

const forTests = process.argv.includes("--test");
const url = forTests ? testDatabaseUrl(configured) : configured;
if (forTests) await ensureDatabase(url);
await runMigrations(url, console.log);
console.log(`migrations up to date${forTests ? " (test database)" : ""}`);
