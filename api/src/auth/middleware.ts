import type { RequestHandler } from "express";
import { findSessionUser, type AuthUser, type Role } from "./sessions.js";

export const SESSION_COOKIE = "sid";

declare global {
  namespace Express {
    interface Request {
      user?: AuthUser;
    }
  }
}

/** Attaches req.user when the session cookie is valid. Never rejects. */
export const loadUser: RequestHandler = async (req, _res, next) => {
  const token: unknown = req.cookies?.[SESSION_COOKIE];
  if (typeof token === "string" && token) {
    req.user = (await findSessionUser(token)) ?? undefined;
  }
  next();
};

export const requireAuth: RequestHandler = (req, res, next) => {
  if (!req.user) {
    res.status(401).json({ error: "unauthenticated" });
    return;
  }
  next();
};

export const requireRole =
  (...roles: Role[]): RequestHandler =>
  (req, res, next) => {
    if (!req.user) {
      res.status(401).json({ error: "unauthenticated" });
      return;
    }
    if (!roles.includes(req.user.role)) {
      res.status(403).json({ error: "forbidden" });
      return;
    }
    next();
  };
