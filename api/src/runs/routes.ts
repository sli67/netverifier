import { Router } from "express";
import { z } from "zod";
import { pool } from "../db.js";
import { MAX_INTENT_LENGTH } from "../investigations/routes.js";

type Status = "queued" | "running" | "waiting_user" | "waiting_approval" | "done" | "failed" | "cancelled";

interface RunRow {
  id: string;
  investigation_id: string;
  mode: "verify" | "change" | "audit";
  intent: string;
  target: string | null;
  status: Status;
  stage: string | null;
  outcome: string | null;
  state: unknown;
  error: string | null;
  created_by: string;
  created_by_email: string;
  created_at: Date;
  updated_at: Date;
  finished_at: Date | null;
  approval: unknown;
  message_id: string | null;
  progress: unknown;
  deployments: unknown;
}

const SELECT = `
  select r.id, r.investigation_id, r.mode, r.intent, r.target, r.status, r.stage, r.outcome, r.state, r.error,
         r.created_by, u.email as created_by_email, r.created_at, r.updated_at, r.finished_at, r.message_id,
         (select j.progress from jobs j where j.run_id = r.id and j.status = 'running'
           order by j.created_at desc limit 1) as progress,
         (select coalesce(jsonb_agg(jsonb_build_object(
                   'id', d.id, 'status', d.status, 'stage', d.stage, 'labName', d.lab_name,
                   'candidateHash', d.candidate_hash, 'log', d.log, 'result', d.result, 'error', d.error,
                   'createdBy', du.email, 'createdAt', d.created_at, 'finishedAt', d.finished_at)
                   order by d.created_at desc), '[]'::jsonb)
            from deployments d join users du on du.id = d.created_by
           where d.run_id = r.id) as deployments,
         (select jsonb_build_object(
                   'decision', a.decision, 'comment', a.comment, 'candidateHash', a.candidate_hash,
                   'decidedBy', au.email, 'decidedAt', a.decided_at)
            from approvals a join users au on au.id = a.decided_by
           where a.run_id = r.id) as approval
    from runs r
    join users u on u.id = r.created_by`;

function present(row: RunRow, withState: boolean) {
  return {
    id: row.id,
    investigationId: row.investigation_id,
    mode: row.mode,
    intent: row.intent,
    // audit runs only: the structure being audited.
    target: row.target,
    status: row.status,
    stage: row.stage,
    outcome: row.outcome,
    error: row.error,
    createdBy: { id: row.created_by, email: row.created_by_email },
    createdAt: row.created_at,
    updatedAt: row.updated_at,
    finishedAt: row.finished_at,
    approval: row.approval,
    // The chat message whose suggestion started this run, if any.
    messageId: row.message_id,
    // What the agent is doing within the current step, while a worker is on the run.
    ...(withState ? { state: row.state, progress: row.progress ?? null, deployments: row.deployments ?? [] } : {}),
  };
}

const ACTIVE = "('queued', 'running', 'waiting_user', 'waiting_approval')";

const createBody = z.object({
  investigationId: z.uuid(),
  mode: z.enum(["verify", "change", "audit"]),
  // audit runs: the exact name of the route-map, policy, list or ACL to audit.
  target: z.string().trim().min(1).max(200).optional(),
  // Saves this as the investigation's intent and runs on it, in one step. Used
  // when a run is started from a suggestion in the chat.
  intent: z.string().trim().min(1).max(MAX_INTENT_LENGTH).optional(),
  // The agent message whose suggestion this run was started from.
  messageId: z.uuid().optional(),
});
const answerBody = z.object({ answer: z.string().trim().min(1).max(4000) });
// The reviewed checks: positions to remove, or a correction that has them rewritten instead.
const checksBody = z.object({
  drop: z.array(z.number().int().min(0)).max(200).default([]),
  note: z.string().trim().max(4000).optional(),
});
const decisionBody = z.object({
  decision: z.enum(["approved", "rejected"]),
  // The content hash of the candidate the reviewer looked at.
  candidateHash: z.string().regex(/^[0-9a-f]{64}$/),
  comment: z.string().trim().max(2000).optional(),
});

async function findRun(id: unknown): Promise<RunRow | null> {
  if (!z.uuid().safeParse(id).success) return null;
  const { rows } = await pool.query<RunRow>(`${SELECT} where r.id = $1`, [id]);
  return rows[0] ?? null;
}

/** Mounted behind requireAuth. */
export function runsRouter(): Router {
  const router = Router();

  // Starts a run of the staged workflow on an investigation's saved intent.
  router.post("/", async (req, res) => {
    const { investigationId, mode, intent, messageId, target } = createBody.parse(req.body);
    if ((mode === "audit") !== Boolean(target)) {
      res.status(400).json({ error: mode === "audit" ? "target_required" : "target_only_for_audit" });
      return;
    }

    const client = await pool.connect();
    let id: string;
    try {
      await client.query("begin");
      const inv = await client.query<{ status: string; intent: string | null }>(
        "select status, intent from investigations where id = $1 for update",
        [investigationId],
      );
      const investigation = inv.rows[0];
      const refusal = !investigation
        ? ([404, "investigation_not_found"] as const)
        : investigation.status !== "ready"
          ? ([409, "not_ready"] as const)
          : mode !== "audit" && !intent && !investigation.intent
            ? ([409, "no_intent"] as const)
            : null;
      if (refusal) {
        await client.query("rollback");
        res.status(refusal[0]).json({ error: refusal[1] });
        return;
      }
      if (messageId) {
        const message = await client.query(
          "select 1 from messages where id = $1 and investigation_id = $2 and role = 'agent'",
          [messageId, investigationId],
        );
        if (message.rowCount === 0) {
          await client.query("rollback");
          res.status(400).json({ error: "message_not_found" });
          return;
        }
      }
      if (intent && mode !== "audit") {
        // Rolled back with everything else if the run cannot start.
        await client.query(
          "update investigations set intent = $2, intent_updated_at = now(), intent_updated_by = $3 where id = $1",
          [investigationId, intent, req.user!.id],
        );
      }
      const inserted = await client.query<{ id: string }>(
        `insert into runs (investigation_id, mode, intent, target, created_by, message_id) values ($1, $2, $3, $4, $5, $6)
         on conflict (investigation_id) where status in ${ACTIVE} do nothing
         returning id`,
        [
          investigationId,
          mode,
          // An audit has no intent of the user's: its intent is inferred from the target.
          mode === "audit" ? `Audit ${target}` : (intent ?? investigation!.intent),
          target ?? null,
          req.user!.id,
          messageId ?? null,
        ],
      );
      if (!inserted.rows[0]) {
        await client.query("rollback");
        res.status(409).json({ error: "run_active" });
        return;
      }
      id = inserted.rows[0].id;
      await client.query(
        `insert into jobs (kind, investigation_id, run_id, payload) values ('run_workflow', $1, $2, '{"action":"start"}')`,
        [investigationId, id],
      );
      await client.query("commit");
    } catch (err) {
      await client.query("rollback");
      throw err;
    } finally {
      client.release();
    }
    res.status(201).json({ run: present((await findRun(id))!, true) });
  });

  // GET /runs?investigationId=...
  router.get("/", async (req, res) => {
    const investigationId = z.uuid().parse(req.query.investigationId);
    const { rows } = await pool.query<RunRow>(
      `${SELECT} where r.investigation_id = $1 order by r.created_at desc limit 50`,
      [investigationId],
    );
    res.json({ runs: rows.map((r) => present(r, false)) });
  });

  router.get("/:id", async (req, res) => {
    const row = await findRun(req.params.id);
    if (!row) {
      res.status(404).json({ error: "not_found" });
      return;
    }
    res.json({ run: present(row, true) });
  });

  /**
   * Moves a waiting run back to running and queues the job that resumes it.
   * `check` may refuse, and may write rows of its own in the same transaction.
   */
  async function resume(
    id: string,
    waitingIn: Status,
    check: (
      run: { created_by: string; stage: string | null; state: any },
      client: import("pg").PoolClient,
    ) => Promise<string | null>,
    payload: Record<string, unknown>,
  ): Promise<{ status: number; error?: string }> {
    const client = await pool.connect();
    try {
      await client.query("begin");
      const found = await client.query<{
        status: Status;
        created_by: string;
        investigation_id: string;
        stage: string | null;
        state: any;
      }>(
        "select status, created_by, investigation_id, stage, state from runs where id = $1 for update",
        [id],
      );
      const run = found.rows[0];
      if (!run) {
        await client.query("rollback");
        return { status: 404, error: "not_found" };
      }
      if (run.status !== waitingIn) {
        await client.query("rollback");
        return { status: 409, error: "not_waiting" };
      }
      const refused = await check(run, client);
      if (refused) {
        await client.query("rollback");
        const status = refused === "forbidden" || refused === "own_run" ? 403 : refused === "unknown_check" ? 400 : 409;
      return { status, error: refused };
      }
      await client.query("update runs set status = 'running', updated_at = now() where id = $1", [id]);
      await client.query(
        "insert into jobs (kind, investigation_id, run_id, payload) values ('run_workflow', $1, $2, $3)",
        [run.investigation_id, id, JSON.stringify(payload)],
      );
      await client.query("commit");
      return { status: 200 };
    } catch (err) {
      await client.query("rollback");
      throw err;
    } finally {
      client.release();
    }
  }

  // Answers the question a run is waiting on.
  router.post("/:id/answer", async (req, res) => {
    const { answer } = answerBody.parse(req.body);
    if (!z.uuid().safeParse(req.params.id).success) {
      res.status(404).json({ error: "not_found" });
      return;
    }
    const user = req.user!;
    const outcome = await resume(
      req.params.id,
      "waiting_user",
      async (run) =>
        run.stage === "review_checks"
          ? "not_waiting"
          : run.created_by === user.id || user.role === "admin"
            ? null
            : "forbidden",
      { action: "answer", answer },
    );
    if (outcome.error) {
      res.status(outcome.status).json({ error: outcome.error });
      return;
    }
    res.json({ run: present((await findRun(req.params.id))!, true) });
  });

  // Confirms the checks a change run will be held to, before anything is proposed.
  // Only the run's starter (or an admin) does this: the checks are their intent.
  router.post("/:id/checks", async (req, res) => {
    const { drop, note } = checksBody.parse(req.body);
    if (!z.uuid().safeParse(req.params.id).success) {
      res.status(404).json({ error: "not_found" });
      return;
    }
    const user = req.user!;
    const outcome = await resume(
      req.params.id,
      "waiting_user",
      async (run) => {
        if (run.stage !== "review_checks") return "not_waiting";
        if (run.created_by !== user.id && user.role !== "admin") return "forbidden";
        const count = Array.isArray(run.state?.checks) ? run.state.checks.length : 0;
        return drop.every((i) => i < count) ? null : "unknown_check";
      },
      { action: "checks", drop: [...new Set(drop)], note: note || null, by: user.email },
    );
    if (outcome.error) {
      res.status(outcome.status).json({ error: outcome.error });
      return;
    }
    res.json({ run: present((await findRun(req.params.id))!, true) });
  });

  // Approves or rejects the candidate a run is waiting on. The decision is
  // recorded against the candidate's content hash, and refused if that is not
  // the candidate that passed validation.
  router.post("/:id/decision", async (req, res) => {
    const { decision, candidateHash, comment } = decisionBody.parse(req.body);
    if (!z.uuid().safeParse(req.params.id).success) {
      res.status(404).json({ error: "not_found" });
      return;
    }
    const user = req.user!;
    const runId = req.params.id;
    const outcome = await resume(
      runId,
      "waiting_approval",
      async (run, client) => {
        if (user.role !== "approver" && user.role !== "admin") return "forbidden";
        // Someone other than the requester reviews a change. Admins are exempt.
        if (run.created_by === user.id && user.role !== "admin") return "own_run";
        const latest = await client.query<{ id: string; content_hash: string }>(
          "select id, content_hash from candidates where run_id = $1 order by round desc limit 1",
          [runId],
        );
        const candidate = latest.rows[0];
        const attempts: { passed: boolean; content_hash?: string }[] = run.state?.attempts ?? [];
        const validated = attempts.at(-1);
        if (
          !candidate ||
          !validated?.passed ||
          candidate.content_hash !== candidateHash ||
          validated.content_hash !== candidateHash
        ) {
          return "candidate_mismatch";
        }
        await client.query(
          `insert into approvals (run_id, candidate_id, candidate_hash, decision, comment, decided_by)
           values ($1, $2, $3, $4, $5, $6)`,
          [runId, candidate.id, candidateHash, decision, comment || null, user.id],
        );
        return null;
      },
      { action: "decision", decision, candidate_hash: candidateHash, by: user.email },
    );
    if (outcome.error) {
      res.status(outcome.status).json({ error: outcome.error });
      return;
    }
    res.json({ run: present((await findRun(runId))!, true) });
  });

  // Deploys an approved run's candidate to the snapshot's lab and verifies it there.
  // A deploy worker does the work; this only records the request and queues it.
  router.post("/:id/deploy", async (req, res) => {
    if (!z.uuid().safeParse(req.params.id).success) {
      res.status(404).json({ error: "not_found" });
      return;
    }
    const user = req.user!;
    if (user.role !== "approver" && user.role !== "admin") {
      res.status(403).json({ error: "forbidden" });
      return;
    }
    const client = await pool.connect();
    let deploymentId: string;
    try {
      await client.query("begin");
      const found = await client.query<{ investigation_id: string; snapshot_id: string; candidate_hash: string | null }>(
        `select r.investigation_id, i.snapshot_id,
                (select a.candidate_hash from approvals a where a.run_id = r.id and a.decision = 'approved') as candidate_hash
           from runs r join investigations i on i.id = r.investigation_id
          where r.id = $1 for update of r`,
        [req.params.id],
      );
      const run = found.rows[0];
      if (!run || !run.candidate_hash) {
        await client.query("rollback");
        res.status(run ? 409 : 404).json({ error: run ? "not_approved" : "not_found" });
        return;
      }
      const candidate = await client.query<{ id: string }>(
        "select id from candidates where run_id = $1 and content_hash = $2 order by round desc limit 1",
        [req.params.id, run.candidate_hash],
      );
      const inserted = await client.query<{ id: string }>(
        `insert into deployments (run_id, snapshot_id, candidate_id, candidate_hash, created_by)
         values ($1, $2, $3, $4, $5)
         on conflict (snapshot_id) where status in ('queued', 'deploying', 'verifying') do nothing
         returning id`,
        [req.params.id, run.snapshot_id, candidate.rows[0]!.id, run.candidate_hash, user.id],
      );
      if (!inserted.rows[0]) {
        await client.query("rollback");
        res.status(409).json({ error: "deployment_active" });
        return;
      }
      deploymentId = inserted.rows[0].id;
      // Each attempt rebuilds the lab from the current configs, so one retry is safe.
      await client.query(
        `insert into jobs (kind, investigation_id, run_id, deployment_id, max_attempts)
         values ('deploy_change', $1, $2, $3, 2)`,
        [run.investigation_id, req.params.id, deploymentId],
      );
      await client.query("commit");
    } catch (err) {
      await client.query("rollback");
      throw err;
    } finally {
      client.release();
    }
    res.status(201).json({ run: present((await findRun(req.params.id))!, true) });
  });

  // Stops a run that is under way or waiting. A worker in the middle of it
  // notices at its next step and discards what it was doing.
  router.post("/:id/cancel", async (req, res) => {
    const row = await findRun(req.params.id);
    if (!row) {
      res.status(404).json({ error: "not_found" });
      return;
    }
    if (row.created_by !== req.user!.id && req.user!.role !== "admin") {
      res.status(403).json({ error: "forbidden" });
      return;
    }
    const updated = await pool.query(
      `update runs set status = 'cancelled', updated_at = now(), finished_at = now()
        where id = $1 and status in ${ACTIVE}`,
      [row.id],
    );
    if (updated.rowCount === 0) {
      res.status(409).json({ error: "not_active" });
      return;
    }
    res.json({ run: present((await findRun(row.id))!, true) });
  });

  return router;
}
