// Usage: npm run create-user -- <email> <requester|approver|admin>
// The password is read from the PASSWORD environment variable, or prompted for.
import { createInterface } from "node:readline/promises";
import { hashPassword, MIN_PASSWORD_LENGTH } from "../auth/passwords.js";
import { pool } from "../db.js";

const ROLES = ["requester", "approver", "admin"];
const [emailArg, role] = process.argv.slice(2);
const email = emailArg?.trim().toLowerCase();

if (!email || !role || !ROLES.includes(role)) {
  console.error(`usage: create-user <email> <${ROLES.join("|")}>`);
  process.exit(1);
}

let password = process.env.PASSWORD;
if (!password) {
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  password = await rl.question("Password: ");
  rl.close();
}
if (password.length < MIN_PASSWORD_LENGTH) {
  console.error(`password must be at least ${MIN_PASSWORD_LENGTH} characters`);
  process.exit(1);
}

try {
  const { rows } = await pool.query<{ id: string }>(
    "insert into users (email, password_hash, role) values ($1, $2, $3) returning id",
    [email, await hashPassword(password), role],
  );
  console.log(`created ${email} (${role}) ${rows[0]?.id}`);
} catch (err) {
  if ((err as { code?: string }).code === "23505") {
    console.error(`user ${email} already exists`);
    process.exitCode = 1;
  } else {
    throw err;
  }
} finally {
  await pool.end();
}
