import { Router } from "express";
import { z } from "zod";
import { pool } from "../db.js";
import { deleteRuns } from "../runs/cleanup.js";
import { deleteObjects } from "../storage.js";

interface InvestigationRow {
  id: string;
  status: "queued" | "parsing" | "ready" | "failed";
  snapshot_id: string;
  snapshot_name: string;
  created_by: string;
  created_by_email: string;
  created_at: Date;
  started_at: Date | null;
  finished_at: Date | null;
  model_reused: boolean | null;
  batfish_instance: string | null;
  batfish_version: string | null;
  parse_ms: number | null;
  result: unknown;
  error: string | null;
  intent: string | null;
  intent_updated_at: Date | null;
  intent_updated_by_email: string | null;
  message_count: number;
  last_message_at: Date | null;
}

const SELECT = `
  select i.id, i.status, i.snapshot_id, s.name as snapshot_name,
         i.created_by, u.email as created_by_email,
         i.created_at, i.started_at, i.finished_at,
         i.model_reused, i.batfish_instance, i.batfish_version, i.parse_ms,
         i.result, i.error,
         i.intent, i.intent_updated_at, iu.email as intent_updated_by_email,
         mc.message_count, mc.last_message_at
    from investigations i
    join snapshots s on s.id = i.snapshot_id
    join users u on u.id = i.created_by
    left join users iu on iu.id = i.intent_updated_by
    cross join lateral (
      select count(*)::int as message_count, max(m.created_at) as last_message_at
        from messages m where m.investigation_id = i.id
    ) mc`;

function present(row: InvestigationRow, withResult: boolean) {
  return {
    id: row.id,
    status: row.status,
    snapshot: { id: row.snapshot_id, name: row.snapshot_name },
    createdBy: { id: row.created_by, email: row.created_by_email },
    createdAt: row.created_at,
    startedAt: row.started_at,
    finishedAt: row.finished_at,
    modelReused: row.model_reused,
    batfishInstance: row.batfish_instance,
    batfishVersion: row.batfish_version,
    parseMs: row.parse_ms,
    error: row.error,
    intent: row.intent,
    intentUpdatedAt: row.intent_updated_at,
    intentUpdatedBy: row.intent_updated_by_email,
    messageCount: row.message_count,
    lastActivityAt: row.last_message_at ?? row.finished_at ?? row.created_at,
    ...(withResult ? { result: row.result } : {}),
  };
}

// `fresh` starts a new investigation even when the user has an earlier one to resume.
const createBody = z.object({ snapshotId: z.uuid(), fresh: z.boolean().optional() });

export const MAX_INTENT_LENGTH = 4000;
const intentBody = z.object({ intent: z.string().trim().max(MAX_INTENT_LENGTH) });

export const MAX_MESSAGE_LENGTH = 4000;
const messageBody = z.object({ content: z.string().trim().min(1).max(MAX_MESSAGE_LENGTH) });

interface MessageRow {
  id: string;
  role: "user" | "agent";
  content: string;
  steps: unknown;
  failed: boolean;
  suggestion: unknown;
  route: "question" | "check" | "change" | "audit" | null;
  duration_ms: number | null;
  model: string | null;
  runs: unknown;
  created_at: Date;
  created_by_email: string | null;
}

const MESSAGE_SELECT = `
  select m.id, m.role, m.content, m.steps, m.failed, m.suggestion, m.route, m.duration_ms, m.model,
         m.created_at, u.email as created_by_email,
         (select coalesce(jsonb_agg(jsonb_build_object(
                   'id', r.id, 'mode', r.mode, 'status', r.status, 'outcome', r.outcome, 'createdAt', r.created_at)
                   order by r.created_at), '[]'::jsonb)
            from runs r where r.message_id = m.id) as runs
    from messages m
    left join users u on u.id = m.created_by`;

function presentMessage(row: MessageRow) {
  return {
    id: row.id,
    role: row.role,
    content: row.content,
    steps: row.steps ?? [],
    failed: row.failed,
    suggestion: row.suggestion ?? null,
    route: row.route,
    durationMs: row.duration_ms,
    model: row.model,
    runs: row.runs ?? [],
    createdAt: row.created_at,
    createdBy: row.created_by_email,
  };
}

async function investigationStatus(id: string): Promise<string | null> {
  const { rows } = await pool.query<{ status: string }>("select status from investigations where id = $1", [id]);
  return rows[0]?.status ?? null;
}

/** Mounted behind requireAuth. */
export function investigationsRouter(): Router {
  const router = Router();

  // Opens an investigation of a snapshot. By default this resumes the user's most
  // recent one that did not fail; otherwise, or with `fresh`, it starts a new one.
  // The work itself is done by a worker that claims the job; this only records
  // the request and returns.
  router.post("/", async (req, res) => {
    const { snapshotId, fresh } = createBody.parse(req.body);

    const client = await pool.connect();
    let id: string;
    let created = false;
    try {
      await client.query("begin");
      const snapshot = await client.query("select 1 from snapshots where id = $1", [snapshotId]);
      if (snapshot.rowCount === 0) {
        await client.query("rollback");
        res.status(404).json({ error: "snapshot_not_found" });
        return;
      }

      if (!fresh) {
        const previous = await client.query<{ id: string }>(
          `select id from investigations
            where snapshot_id = $1 and created_by = $2 and status <> 'failed'
            order by created_at desc limit 1`,
          [snapshotId, req.user!.id],
        );
        if (previous.rows[0]) {
          await client.query("commit");
          const { rows } = await pool.query<InvestigationRow>(`${SELECT} where i.id = $1`, [previous.rows[0].id]);
          res.status(200).json({ investigation: present(rows[0]!, true) });
          return;
        }
      }

      // The partial unique index allows one active investigation per snapshot,
      // so a double click or a second user gets the one already under way.
      const inserted = await client.query<{ id: string }>(
        `insert into investigations (snapshot_id, created_by) values ($1, $2)
         on conflict (snapshot_id) where status in ('queued', 'parsing') do nothing
         returning id`,
        [snapshotId, req.user!.id],
      );
      if (inserted.rows[0]) {
        id = inserted.rows[0].id;
        created = true;
        await client.query("insert into jobs (kind, investigation_id) values ('parse_snapshot', $1)", [id]);
      } else {
        const active = await client.query<{ id: string }>(
          "select id from investigations where snapshot_id = $1 and status in ('queued', 'parsing')",
          [snapshotId],
        );
        if (!active.rows[0]) {
          // The active one finished between the insert and this read.
          await client.query("rollback");
          res.status(409).json({ error: "try_again" });
          return;
        }
        id = active.rows[0].id;
      }
      await client.query("commit");
    } catch (err) {
      await client.query("rollback");
      throw err;
    } finally {
      client.release();
    }

    const { rows } = await pool.query<InvestigationRow>(`${SELECT} where i.id = $1`, [id]);
    res.status(created ? 201 : 200).json({ investigation: present(rows[0]!, true) });
  });

  // GET /investigations?snapshotId=...
  router.get("/", async (req, res) => {
    const snapshotId = z.uuid().optional().parse(req.query.snapshotId);
    const { rows } = await pool.query<InvestigationRow>(
      `${SELECT} where ($1::uuid is null or i.snapshot_id = $1) order by i.created_at desc limit 100`,
      [snapshotId ?? null],
    );
    res.json({ investigations: rows.map((r) => present(r, false)) });
  });

  router.get("/:id", async (req, res) => {
    if (!z.uuid().safeParse(req.params.id).success) {
      res.status(404).json({ error: "not_found" });
      return;
    }
    const { rows } = await pool.query<InvestigationRow>(`${SELECT} where i.id = $1`, [req.params.id]);
    if (!rows[0]) {
      res.status(404).json({ error: "not_found" });
      return;
    }
    res.json({ investigation: present(rows[0], true) });
  });

  // Deletes an investigation and its conversation. The snapshot is kept.
  router.delete("/:id", async (req, res) => {
    if (!z.uuid().safeParse(req.params.id).success) {
      res.status(404).json({ error: "not_found" });
      return;
    }
    const client = await pool.connect();
    let candidateKeys: string[] = [];
    try {
      await client.query("begin");
      const found = await client.query<{ created_by: string }>(
        "select created_by from investigations where id = $1 for update",
        [req.params.id],
      );
      const owner = found.rows[0]?.created_by;
      if (!owner || (owner !== req.user!.id && req.user!.role !== "admin")) {
        await client.query("rollback");
        res.status(owner ? 403 : 404).json({ error: owner ? "forbidden" : "not_found" });
        return;
      }
      await client.query("delete from messages where investigation_id = $1", [req.params.id]);
      // A worker holding one of these jobs finds it gone and discards its result.
      await client.query("delete from jobs where investigation_id = $1", [req.params.id]);
      candidateKeys = await deleteRuns(client, "select $1::uuid", req.params.id);
      await client.query("delete from investigations where id = $1", [req.params.id]);
      await client.query("commit");
    } catch (err) {
      await client.query("rollback");
      throw err;
    } finally {
      client.release();
    }
    try {
      await deleteObjects(candidateKeys);
    } catch (err) {
      console.error(`investigation ${req.params.id} deleted, but its candidate files could not be removed`, err);
    }
    res.status(204).end();
  });

  // Saves the user's description of the change they want. An empty string clears it.
  router.put("/:id/intent", async (req, res) => {
    if (!z.uuid().safeParse(req.params.id).success) {
      res.status(404).json({ error: "not_found" });
      return;
    }
    const { intent } = intentBody.parse(req.body);
    const updated = await pool.query(
      `update investigations
          set intent = $2, intent_updated_at = now(), intent_updated_by = $3
        where id = $1`,
      [req.params.id, intent === "" ? null : intent, req.user!.id],
    );
    if (updated.rowCount === 0) {
      res.status(404).json({ error: "not_found" });
      return;
    }
    const { rows } = await pool.query<InvestigationRow>(`${SELECT} where i.id = $1`, [req.params.id]);
    res.json({ investigation: present(rows[0]!, true) });
  });

  // The conversation with the agent, and whether a reply is still being produced.
  router.get("/:id/messages", async (req, res) => {
    if (!z.uuid().safeParse(req.params.id).success || !(await investigationStatus(req.params.id))) {
      res.status(404).json({ error: "not_found" });
      return;
    }
    const messages = await pool.query<MessageRow>(`${MESSAGE_SELECT} where m.investigation_id = $1 order by m.created_at, m.id`, [
      req.params.id,
    ]);
    const pending = await pool.query<{ status: string; progress: unknown }>(
      `select status, progress from jobs
        where investigation_id = $1 and kind = 'agent_reply' and status in ('queued', 'running')
        order by created_at desc limit 1`,
      [req.params.id],
    );
    const job = pending.rows[0];
    res.json({
      messages: messages.rows.map(presentMessage),
      agentPending: Boolean(job),
      // What the agent is doing right now: { step, doing }. Null while the reply is queued.
      agentProgress: job?.status === "running" ? (job.progress ?? null) : null,
    });
  });

  // Posts a user message and queues the agent's reply. One reply at a time per investigation.
  router.post("/:id/messages", async (req, res) => {
    if (!z.uuid().safeParse(req.params.id).success) {
      res.status(404).json({ error: "not_found" });
      return;
    }
    const { content } = messageBody.parse(req.body);

    const client = await pool.connect();
    let messageId: string;
    try {
      await client.query("begin");
      // Locking the investigation row serialises concurrent posts to the same conversation.
      const inv = await client.query<{ status: string }>("select status from investigations where id = $1 for update", [
        req.params.id,
      ]);
      const status = inv.rows[0]?.status;
      const busy =
        status === "ready" &&
        (
          await client.query(
            "select 1 from jobs where investigation_id = $1 and kind = 'agent_reply' and status in ('queued', 'running')",
            [req.params.id],
          )
        ).rowCount! > 0;
      if (status !== "ready" || busy) {
        await client.query("rollback");
        if (!status) res.status(404).json({ error: "not_found" });
        else res.status(409).json({ error: busy ? "agent_busy" : "not_ready" });
        return;
      }

      const inserted = await client.query<{ id: string }>(
        "insert into messages (investigation_id, role, content, created_by) values ($1, 'user', $2, $3) returning id",
        [req.params.id, content, req.user!.id],
      );
      messageId = inserted.rows[0]!.id;
      await client.query("insert into jobs (kind, investigation_id) values ('agent_reply', $1)", [req.params.id]);
      await client.query("commit");
    } catch (err) {
      await client.query("rollback");
      throw err;
    } finally {
      client.release();
    }

    const { rows } = await pool.query<MessageRow>(`${MESSAGE_SELECT} where m.id = $1`, [messageId]);
    res.status(201).json({ message: presentMessage(rows[0]!) });
  });

  return router;
}
