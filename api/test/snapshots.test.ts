import { createHash, randomUUID } from "node:crypto";
import request from "supertest";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { createApp } from "../src/app.js";
import { hashPassword } from "../src/auth/passwords.js";
import { pool } from "../src/db.js";
import { getText, putText } from "../src/storage.js";

const email = `test-${randomUUID()}@example.test`;
const password = "correct horse battery staple";
const agent = request.agent(createApp());

const R1 = "router bgp 65001\n neighbor 10.0.0.2 remote-as 65002\n";
const R2 = "router bgp 65002\n neighbor 10.0.0.1 remote-as 65001\n";
const upload = (files: unknown, name = "lab baseline") => agent.post("/snapshots").send({ name, files });

beforeAll(async () => {
  await pool.query("insert into users (email, password_hash, role) values ($1, $2, 'requester')", [
    email,
    await hashPassword(password),
  ]);
  await agent.post("/auth/login").send({ email, password }).expect(200);
});

afterAll(async () => {
  const mine = "(select id from users where email = $1)";
  await pool.query(`delete from messages where investigation_id in (select id from investigations where created_by = ${mine})`, [email]);
  await pool.query(`delete from jobs where investigation_id in (select id from investigations where created_by = ${mine})`, [email]);
  await pool.query(`delete from investigations where created_by = ${mine}`, [email]);
  await pool.query("delete from snapshots where created_by = (select id from users where email = $1)", [email]);
  await pool.query("delete from users where email = $1", [email]);
  await pool.end();
});

describe("snapshots", () => {
  it("requires authentication", async () => {
    const app = createApp();
    expect((await request(app).get("/snapshots")).status).toBe(401);
    const res = await request(app).post("/snapshots").send({ name: "x", files: [{ path: "a", content: "b" }] });
    expect(res.status).toBe(401);
  });

  it("uploads a snapshot and reads it back", async () => {
    const created = await upload([
      { path: "r2/frr.conf", content: R2 },
      { path: "r1/frr.conf", content: R1 },
    ]);
    expect(created.status).toBe(201);
    const snap = created.body.snapshot;
    expect(snap).toMatchObject({ name: "lab baseline", fileCount: 2, totalBytes: R1.length + R2.length });
    expect(snap.createdBy.email).toBe(email);
    expect(snap.files.map((f: { path: string }) => f.path)).toEqual(["r1/frr.conf", "r2/frr.conf"]);
    expect(snap.files[0].sha256).toBe(createHash("sha256").update(R1).digest("hex"));

    const list = await agent.get("/snapshots");
    const listed = list.body.snapshots.find((s: { id: string }) => s.id === snap.id);
    expect(listed).toMatchObject({ id: snap.id, contentHash: snap.contentHash });
    expect(listed.files).toBeUndefined();

    const one = await agent.get(`/snapshots/${snap.id}`);
    expect(one.body.snapshot.files).toHaveLength(2);

    const file = await agent.get(`/snapshots/${snap.id}/file`).query({ path: "r1/frr.conf" });
    expect(file.status).toBe(200);
    expect(file.headers["content-type"]).toMatch(/^text\/plain/);
    expect(file.text).toBe(R1);
  });

  it("gives identical content the same hash regardless of upload order", async () => {
    const a = await upload([
      { path: "r1/frr.conf", content: R1 },
      { path: "r2/frr.conf", content: R2 },
    ]);
    const b = await upload([
      { path: "r2/frr.conf", content: R2 },
      { path: "r1/frr.conf", content: R1 },
    ]);
    const c = await upload([
      { path: "r1/frr.conf", content: R1 },
      { path: "r2/frr.conf", content: R2 + "!\n" },
    ]);
    expect(a.body.snapshot.contentHash).toBe(b.body.snapshot.contentHash);
    expect(a.body.snapshot.id).not.toBe(b.body.snapshot.id);
    expect(c.body.snapshot.contentHash).not.toBe(a.body.snapshot.contentHash);
  });

  it.each([
    ["path traversal", [{ path: "../etc/passwd", content: "x" }]],
    ["absolute path", [{ path: "/etc/passwd", content: "x" }]],
    ["hidden segment", [{ path: "r1/.secret", content: "x" }]],
    ["empty segment", [{ path: "r1//frr.conf", content: "x" }]],
    ["empty content", [{ path: "r1/frr.conf", content: "" }]],
    ["no files", []],
    [
      "duplicate path",
      [
        { path: "r1/frr.conf", content: "a" },
        { path: "R1/frr.conf", content: "b" },
      ],
    ],
    ["oversized file", [{ path: "r1/frr.conf", content: "x".repeat(512 * 1024 + 1) }]],
  ])("rejects %s", async (_label, files) => {
    const res = await upload(files);
    expect(res.status).toBe(400);
    expect(res.body.error).toBe("invalid_request");
  });

  it("rejects a body over the size limit", async () => {
    const files = Array.from({ length: 20 }, (_, i) => ({ path: `r${i}/frr.conf`, content: "x".repeat(500 * 1024) }));
    const res = await upload(files);
    expect(res.status).toBe(413);
  });

  it("returns 404 for unknown snapshots and unknown files", async () => {
    expect((await agent.get(`/snapshots/${randomUUID()}`)).status).toBe(404);
    expect((await agent.get("/snapshots/not-a-uuid")).status).toBe(404);

    const { body } = await upload([{ path: "r1/frr.conf", content: R1 }]);
    const missing = await agent.get(`/snapshots/${body.snapshot.id}/file`).query({ path: "r9/frr.conf" });
    expect(missing.status).toBe(404);
  });

  describe("delete", () => {
    const others: string[] = [];
    async function signIn(role: string) {
      const address = `test-${randomUUID()}@example.test`;
      others.push(address);
      await pool.query("insert into users (email, password_hash, role) values ($1, $2, $3)", [
        address,
        await hashPassword(password),
        role,
      ]);
      const other = request.agent(createApp());
      await other.post("/auth/login").send({ email: address, password }).expect(200);
      return other;
    }

    afterAll(async () => {
      await pool.query("delete from users where email = any($1)", [others]);
    });

    it("removes the snapshot, its investigations and conversations, and its stored files", async () => {
      const { body } = await upload([
        { path: "r1/frr.conf", content: R1 },
        { path: "r2/frr.conf", content: R2 },
      ]);
      const id = body.snapshot.id;
      const inv = await agent.post("/investigations").send({ snapshotId: id }).expect(201);
      const invId = inv.body.investigation.id;
      await pool.query("update investigations set status = 'ready' where id = $1", [invId]);
      await agent.post(`/investigations/${invId}/messages`).send({ content: "hello" }).expect(201);

      expect((await agent.delete(`/snapshots/${id}`)).status).toBe(204);

      expect((await agent.get(`/snapshots/${id}`)).status).toBe(404);
      expect((await agent.get(`/investigations/${invId}`)).status).toBe(404);
      const left = await pool.query(
        `select (select count(*) from investigations where snapshot_id = $1)::int as investigations,
                (select count(*) from jobs where investigation_id = $2)::int as jobs,
                (select count(*) from messages where investigation_id = $2)::int as messages`,
        [id, invId],
      );
      expect(left.rows[0]).toEqual({ investigations: 0, jobs: 0, messages: 0 });
      await expect(getText(`snapshots/${id}/configs/r1/frr.conf`)).rejects.toThrow();
      await expect(getText(`snapshots/${id}/configs/r2/frr.conf`)).rejects.toThrow();

      expect((await agent.delete(`/snapshots/${id}`)).status).toBe(404);
    });

    it("is refused for other users, allowed for admins, and needs a session", async () => {
      const { body } = await upload([{ path: "r1/frr.conf", content: R1 }]);
      const id = body.snapshot.id;

      expect((await request(createApp()).delete(`/snapshots/${id}`)).status).toBe(401);
      expect((await (await signIn("approver")).delete(`/snapshots/${id}`)).status).toBe(403);
      expect((await agent.get(`/snapshots/${id}`)).status).toBe(200);

      expect((await (await signIn("admin")).delete(`/snapshots/${id}`)).status).toBe(204);
      expect((await agent.get(`/snapshots/${id}`)).status).toBe(404);
    });

    it("returns 404 for unknown ids", async () => {
      expect((await agent.delete(`/snapshots/${randomUUID()}`)).status).toBe(404);
      expect((await agent.delete("/snapshots/not-a-uuid")).status).toBe(404);
    });
  });

  it("refuses to serve a file whose stored content no longer matches its hash", async () => {
    const { body } = await upload([{ path: "r1/frr.conf", content: R1 }]);
    await putText(`snapshots/${body.snapshot.id}/configs/r1/frr.conf`, "tampered\n");
    const res = await agent.get(`/snapshots/${body.snapshot.id}/file`).query({ path: "r1/frr.conf" });
    expect(res.status).toBe(500);
    expect(res.text).not.toContain("tampered");
  });
});
