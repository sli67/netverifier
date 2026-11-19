import cookieParser from "cookie-parser";
import express, { type ErrorRequestHandler, type Express } from "express";
import helmet from "helmet";
import { ZodError } from "zod";
import { loadUser, requireAuth } from "./auth/middleware.js";
import { authRouter } from "./auth/routes.js";
import { investigationsRouter } from "./investigations/routes.js";
import { runsRouter } from "./runs/routes.js";
import { snapshotsRouter } from "./snapshots/routes.js";

export function createApp(): Express {
  const app = express();

  app.disable("x-powered-by");
  app.use(helmet());
  app.use(cookieParser());

  app.get("/healthz", (_req, res) => {
    res.json({ ok: true });
  });

  app.use(loadUser);

  // JSON bodies only: cross-site forms cannot send this content type without a
  // CORS preflight, which together with SameSite=Lax cookies covers CSRF.
  // Snapshot uploads get a larger limit, and only after the caller is authenticated.
  app.use("/snapshots", requireAuth, express.json({ limit: "8mb" }), snapshotsRouter());

  app.use(express.json({ limit: "1mb" }));
  app.use("/auth", authRouter());
  app.use("/investigations", requireAuth, investigationsRouter());
  app.use("/runs", requireAuth, runsRouter());

  app.use((_req, res) => {
    res.status(404).json({ error: "not_found" });
  });

  const onError: ErrorRequestHandler = (err, _req, res, _next) => {
    if (err instanceof ZodError) {
      res.status(400).json({ error: "invalid_request", issues: err.issues });
      return;
    }
    const status = typeof err?.status === "number" ? err.status : 500;
    if (status >= 500) console.error(err);
    const error = status >= 500 ? "internal_error" : status === 413 ? "payload_too_large" : "bad_request";
    res.status(status).json({ error });
  };
  app.use(onError);

  return app;
}
