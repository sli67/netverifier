import { readFileSync } from "node:fs";
import { parseEnv } from "node:util";
import { testDatabaseUrl } from "../src/migrations.js";

const fileEnv = parseEnv(readFileSync(".env", "utf8"));
if (!fileEnv.DATABASE_URL) throw new Error("DATABASE_URL is missing from api/.env");

/** Tests never touch the development database: they get their own, suffixed _test. */
export const TEST_DATABASE_URL = testDatabaseUrl(fileEnv.DATABASE_URL);
export const testEnv = { ...fileEnv, NODE_ENV: "test", DATABASE_URL: TEST_DATABASE_URL };
