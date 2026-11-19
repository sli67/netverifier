import { Router, type CookieOptions } from "express";
import { rateLimit } from "express-rate-limit";
import { z } from "zod";
import { config } from "../config.js";
import { pool } from "../db.js";
import { requireAuth, SESSION_COOKIE } from "./middleware.js";
import { getDummyHash, verifyPassword } from "./passwords.js";
import { createSession, deleteSession, type Role } from "./sessions.js";

const cookieOptions: CookieOptions = {
  httpOnly: true,
  sameSite: "lax",
  secure: config.NODE_ENV === "production",
  path: "/",
};

const loginBody = z.object({
  email: z.string().trim().toLowerCase().min(1).max(320),
  password: z.string().min(1).max(1024),
});

interface UserRow {
  id: string;
  email: string;
  role: Role;
  password_hash: string;
  disabled_at: Date | null;
}

export function authRouter(): Router {
  const router = Router();

  const loginLimiter = rateLimit({
    windowMs: 15 * 60 * 1000,
    limit: 10,
    standardHeaders: "draft-7",
    legacyHeaders: false,
    message: { error: "too_many_attempts" },
  });

  router.post("/login", loginLimiter, async (req, res) => {
    const { email, password } = loginBody.parse(req.body);

    const { rows } = await pool.query<UserRow>(
      "select id, email, role, password_hash, disabled_at from users where email = $1",
      [email],
    );
    const user = rows[0];
    const ok = await verifyPassword(user?.password_hash ?? (await getDummyHash()), password);

    if (!user || !ok || user.disabled_at) {
      res.status(401).json({ error: "invalid_credentials" });
      return;
    }

    const token = await createSession(user.id);
    res.cookie(SESSION_COOKIE, token, {
      ...cookieOptions,
      maxAge: config.SESSION_TTL_HOURS * 60 * 60 * 1000,
    });
    res.json({ user: { id: user.id, email: user.email, role: user.role } });
  });

  router.post("/logout", async (req, res) => {
    const token: unknown = req.cookies?.[SESSION_COOKIE];
    if (typeof token === "string" && token) await deleteSession(token);
    res.clearCookie(SESSION_COOKIE, cookieOptions);
    res.status(204).end();
  });

  router.get("/me", requireAuth, (req, res) => {
    res.json({ user: req.user });
  });

  return router;
}
