import { createHash, randomUUID } from "node:crypto";
import { Router } from "express";
import { z } from "zod";
import { pool } from "../db.js";
import { deleteRuns } from "../runs/cleanup.js";
import { deleteObjects, getText, putText } from "../storage.js";

export const MAX_FILES = 200;
export const MAX_FILE_BYTES = 512 * 1024;
export const MAX_TOTAL_BYTES = 5 * 1024 * 1024;
const MAX_PATH_DEPTH = 4;

// Each segment starts with a letter or digit, which rules out "", "." and "..".
const PATH_SEGMENT = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

const filePath = z
  .string()
  .max(200)
  .refine(
    (p) => {
      const segments = p.split("/");
      return segments.length <= MAX_PATH_DEPTH && segments.every((s) => PATH_SEGMENT.test(s));
    },
    { message: "must be a relative path of letters, digits, '.', '_' and '-'" },
  );

const createBody = z
  .object({
    name: z.string().trim().min(1).max(120),
    files: z
      .array(z.object({ path: filePath, content: z.string().min(1) }))
      .min(1)
      .max(MAX_FILES),
  })
  .superRefine((body, ctx) => {
    const seen = new Set<string>();
    let total = 0;
    body.files.forEach((file, i) => {
      // Compared case-insensitively so a snapshot unpacks cleanly on any filesystem.
      const key = file.path.toLowerCase();
      if (seen.has(key)) {
        ctx.addIssue({ code: "custom", path: ["files", i, "path"], message: "duplicate path" });
      }
      seen.add(key);
      const size = Buffer.byteLength(file.content, "utf8");
      total += size;
      if (size > MAX_FILE_BYTES) {
        ctx.addIssue({ code: "custom", path: ["files", i, "content"], message: "file too large" });
      }
    });
    if (total > MAX_TOTAL_BYTES) {
      ctx.addIssue({ code: "custom", path: ["files"], message: "snapshot too large" });
    }
  });

interface FileEntry {
  path: string;
  sha256: string;
  size: number;
}

interface SnapshotRow {
  id: string;
  name: string;
  s3_prefix: string;
  content_hash: string;
  files: FileEntry[];
  file_count: number;
  total_bytes: number;
  created_at: Date;
  created_by: string;
  created_by_email: string;
  resume_investigation_id: string | null;
}

// `viewer` is the placeholder holding the signed-in user's id. It selects the
// investigation that user would resume: their most recent one that did not fail.
const select = (viewer: string) => `
  select s.id, s.name, s.s3_prefix, s.content_hash, s.files, s.file_count, s.total_bytes,
         s.created_at, s.created_by, u.email as created_by_email,
         (select i.id from investigations i
           where i.snapshot_id = s.id and i.created_by = ${viewer}::uuid and i.status <> 'failed'
           order by i.created_at desc limit 1) as resume_investigation_id
    from snapshots s
    join users u on u.id = s.created_by`;

const sha256 = (text: string) => createHash("sha256").update(text, "utf8").digest("hex");

function present(row: SnapshotRow, withFiles: boolean) {
  return {
    id: row.id,
    name: row.name,
    contentHash: row.content_hash,
    fileCount: row.file_count,
    totalBytes: row.total_bytes,
    createdAt: row.created_at,
    createdBy: { id: row.created_by, email: row.created_by_email },
    resumeInvestigationId: row.resume_investigation_id,
    ...(withFiles ? { files: row.files } : {}),
  };
}

async function findSnapshot(id: unknown, viewerId: string): Promise<SnapshotRow | null> {
  if (!z.uuid().safeParse(id).success) return null;
  const { rows } = await pool.query<SnapshotRow>(`${select("$2")} where s.id = $1`, [id, viewerId]);
  return rows[0] ?? null;
}

/** Mounted behind requireAuth. A snapshot's content never changes; it can only be deleted whole. */
export function snapshotsRouter(): Router {
  const router = Router();

  router.post("/", async (req, res) => {
    const body = createBody.parse(req.body);
    const id = randomUUID();
    const prefix = `snapshots/${id}/`;

    const files: FileEntry[] = body.files
      .map((f) => ({ path: f.path, sha256: sha256(f.content), size: Buffer.byteLength(f.content, "utf8") }))
      .sort((a, b) => (a.path < b.path ? -1 : 1));
    const contentHash = sha256(files.map((f) => `${f.sha256}  ${f.path}\n`).join(""));
    const totalBytes = files.reduce((sum, f) => sum + f.size, 0);

    // Objects first, row second: a failure leaves unreferenced objects, never a
    // snapshot row that points at missing files.
    await Promise.all(body.files.map((f) => putText(`${prefix}configs/${f.path}`, f.content)));

    await pool.query(
      `insert into snapshots (id, name, s3_prefix, content_hash, files, file_count, total_bytes, created_by)
       values ($1, $2, $3, $4, $5, $6, $7, $8)`,
      [id, body.name, prefix, contentHash, JSON.stringify(files), files.length, totalBytes, req.user!.id],
    );

    const row = await findSnapshot(id, req.user!.id);
    res.status(201).json({ snapshot: present(row!, true) });
  });

  router.get("/", async (req, res) => {
    const { rows } = await pool.query<SnapshotRow>(`${select("$1")} order by s.created_at desc limit 200`, [
      req.user!.id,
    ]);
    res.json({ snapshots: rows.map((r) => present(r, false)) });
  });

  router.get("/:id", async (req, res) => {
    const row = await findSnapshot(req.params.id, req.user!.id);
    if (!row) {
      res.status(404).json({ error: "not_found" });
      return;
    }
    res.json({ snapshot: present(row, true) });
  });

  // GET /snapshots/:id/file?path=r1/frr.conf
  router.get("/:id/file", async (req, res) => {
    const row = await findSnapshot(req.params.id, req.user!.id);
    // The path is looked up in the recorded manifest, never used to build a key directly.
    const entry = row?.files.find((f) => f.path === req.query.path);
    if (!row || !entry) {
      res.status(404).json({ error: "not_found" });
      return;
    }

    const content = await getText(`${row.s3_prefix}configs/${entry.path}`);
    if (sha256(content) !== entry.sha256) {
      throw new Error(`integrity check failed for snapshot ${row.id} file ${entry.path}`);
    }
    res.type("text/plain").send(content);
  });

  // Deletes a snapshot together with its investigations and their conversations.
  // Allowed for the uploader and for admins. This cannot be undone.
  router.delete("/:id", async (req, res) => {
    const row = await findSnapshot(req.params.id, req.user!.id);
    if (!row) {
      res.status(404).json({ error: "not_found" });
      return;
    }
    if (row.created_by !== req.user!.id && req.user!.role !== "admin") {
      res.status(403).json({ error: "forbidden" });
      return;
    }

    const client = await pool.connect();
    let candidateKeys: string[] = [];
    try {
      await client.query("begin");
      const investigations = "select id from investigations where snapshot_id = $1";
      await client.query(`delete from messages where investigation_id in (${investigations})`, [row.id]);
      // A worker holding one of these jobs finds it gone and discards its result.
      await client.query(`delete from jobs where investigation_id in (${investigations})`, [row.id]);
      candidateKeys = await deleteRuns(client, investigations, row.id);
      await client.query("delete from investigations where snapshot_id = $1", [row.id]);
      await client.query("delete from snapshots where id = $1", [row.id]);
      await client.query("commit");
    } catch (err) {
      await client.query("rollback");
      throw err;
    } finally {
      client.release();
    }

    // Rows first, objects second: a failure here leaves unreferenced objects,
    // never a snapshot that points at missing files.
    try {
      await deleteObjects([...row.files.map((f) => `${row.s3_prefix}configs/${f.path}`), ...candidateKeys]);
    } catch (err) {
      console.error(`snapshot ${row.id} deleted, but its stored files could not be removed`, err);
    }
    res.status(204).end();
  });

  return router;
}
