import { createHash, randomBytes } from "node:crypto";
import { config } from "../config.js";
import { pool } from "../db.js";

export type Role = "requester" | "approver" | "admin";

export interface AuthUser {
  id: string;
  email: string;
  role: Role;
}

const hashToken = (token: string) => createHash("sha256").update(token).digest();

export async function createSession(userId: string): Promise<string> {
  const token = randomBytes(32).toString("base64url");
  await pool.query("delete from sessions where user_id = $1 and expires_at <= now()", [userId]);
  await pool.query(
    "insert into sessions (token_hash, user_id, expires_at) values ($1, $2, now() + $3 * interval '1 hour')",
    [hashToken(token), userId, config.SESSION_TTL_HOURS],
  );
  return token;
}

export async function findSessionUser(token: string): Promise<AuthUser | null> {
  const { rows } = await pool.query<AuthUser>(
    `select u.id, u.email, u.role
       from sessions s
       join users u on u.id = s.user_id
      where s.token_hash = $1
        and s.expires_at > now()
        and u.disabled_at is null`,
    [hashToken(token)],
  );
  return rows[0] ?? null;
}

export async function deleteSession(token: string): Promise<void> {
  await pool.query("delete from sessions where token_hash = $1", [hashToken(token)]);
}
