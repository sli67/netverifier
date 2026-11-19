import { randomUUID } from "node:crypto";
import request from "supertest";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { createApp } from "../src/app.js";
import { hashPassword } from "../src/auth/passwords.js";
import { pool } from "../src/db.js";

const email = `test-${randomUUID()}@example.test`;
const password = "correct horse battery staple";
const agent = request.agent(createApp());
let snapshotId: string;

const mine = "created_by = (select id from users where email = $1)";

beforeAll(async () => {
  await pool.query("insert into users (email, password_hash, role) values ($1, $2, 'requester')", [
    email,
    await hashPassword(password),
  ]);
  await agent.post("/auth/login").send({ email, password }).expect(200);
  const res = await agent
    .post("/snapshots")
    .send({ name: "for investigation", files: [{ path: "r1/frr.conf", content: "router bgp 65001\n" }] })
    .expect(201);
  snapshotId = res.body.snapshot.id;
});

afterAll(async () => {
  await pool.query(`delete from messages where investigation_id in (select id from investigations where ${mine})`, [email]);
  await pool.query(`delete from jobs where investigation_id in (select id from investigations where ${mine})`, [email]);
  await pool.query(`delete from investigations where ${mine}`, [email]);
  await pool.query(`delete from snapshots where ${mine}`, [email]);
  await pool.query("delete from users where email = $1", [email]);
  await pool.end();
});

describe("investigations", () => {
  it("requires authentication", async () => {
    const app = createApp();
    expect((await request(app).post("/investigations").send({ snapshotId })).status).toBe(401);
    expect((await request(app).get("/investigations")).status).toBe(401);
  });

  it("rejects unknown and malformed snapshot ids", async () => {
    expect((await agent.post("/investigations").send({ snapshotId: randomUUID() })).status).toBe(404);
    expect((await agent.post("/investigations").send({ snapshotId: "nope" })).status).toBe(400);
    expect((await agent.post("/investigations").send({})).status).toBe(400);
  });

  it("queues one investigation and one job, and returns the active one on repeat", async () => {
    const [a, b, c] = await Promise.all([
      agent.post("/investigations").send({ snapshotId }),
      agent.post("/investigations").send({ snapshotId }),
      agent.post("/investigations").send({ snapshotId }),
    ]);
    const statuses = [a.status, b.status, c.status].sort();
    expect(statuses).toEqual([200, 200, 201]);
    const id = a.body.investigation.id;
    expect(b.body.investigation.id).toBe(id);
    expect(c.body.investigation.id).toBe(id);
    expect(a.body.investigation).toMatchObject({
      status: "queued",
      snapshot: { id: snapshotId, name: "for investigation" },
      createdBy: { email },
    });

    const jobs = await pool.query("select kind, status, attempts from jobs where investigation_id = $1", [id]);
    expect(jobs.rows).toEqual([{ kind: "parse_snapshot", status: "queued", attempts: 0 }]);

    const one = await agent.get(`/investigations/${id}`);
    expect(one.body.investigation.status).toBe("queued");

    const list = await agent.get("/investigations").query({ snapshotId });
    expect(list.body.investigations.map((i: { id: string }) => i.id)).toEqual([id]);
    expect(list.body.investigations[0].result).toBeUndefined();
  });

  it("resumes the user's previous investigation by default, and starts a new one when asked", async () => {
    const first = await agent.post("/investigations").send({ snapshotId });
    const firstId = first.body.investigation.id;
    await pool.query("update investigations set status = 'ready', finished_at = now() where id = $1", [firstId]);

    const resumed = await agent.post("/investigations").send({ snapshotId });
    expect(resumed.status).toBe(200);
    expect(resumed.body.investigation.id).toBe(firstId);
    expect(resumed.body.investigation).toMatchObject({ messageCount: 0 });

    const fresh = await agent.post("/investigations").send({ snapshotId, fresh: true });
    expect(fresh.status).toBe(201);
    expect(fresh.body.investigation.id).not.toBe(firstId);

    // The newest one is now the one resumed.
    const again = await agent.post("/investigations").send({ snapshotId });
    expect(again.body.investigation.id).toBe(fresh.body.investigation.id);
  });

  it("does not resume a failed investigation", async () => {
    const latest = await agent.post("/investigations").send({ snapshotId });
    await pool.query(`update investigations set status = 'failed' where snapshot_id = $1`, [snapshotId]);
    const next = await agent.post("/investigations").send({ snapshotId });
    expect(next.status).toBe(201);
    expect(next.body.investigation.id).not.toBe(latest.body.investigation.id);
    await pool.query("update investigations set status = 'ready' where id = $1", [next.body.investigation.id]);
  });

  it("does not resume another user's investigation", async () => {
    const otherEmail = `test-${randomUUID()}@example.test`;
    await pool.query("insert into users (email, password_hash, role) values ($1, $2, 'requester')", [
      otherEmail,
      await hashPassword(password),
    ]);
    const other = request.agent(createApp());
    await other.post("/auth/login").send({ email: otherEmail, password }).expect(200);
    const mineNow = await agent.post("/investigations").send({ snapshotId });
    const theirs = await other.post("/investigations").send({ snapshotId });
    try {
      expect(theirs.status).toBe(201);
      expect(theirs.body.investigation.id).not.toBe(mineNow.body.investigation.id);
    } finally {
      await pool.query("delete from jobs where investigation_id = $1", [theirs.body.investigation.id]);
      await pool.query("delete from investigations where id = $1", [theirs.body.investigation.id]);
      await pool.query("delete from sessions where user_id = (select id from users where email = $1)", [otherEmail]);
      await pool.query("delete from users where email = $1", [otherEmail]);
    }
  });

  it("saves, replaces and clears the intent", async () => {
    const { body } = await agent.post("/investigations").send({ snapshotId });
    const id = body.investigation.id;
    expect(body.investigation.intent).toBeNull();

    const saved = await agent.put(`/investigations/${id}/intent`).send({ intent: "  Allow 192.0.2.0/24 to ISP-A.  " });
    expect(saved.status).toBe(200);
    expect(saved.body.investigation).toMatchObject({ intent: "Allow 192.0.2.0/24 to ISP-A.", intentUpdatedBy: email });
    expect(saved.body.investigation.intentUpdatedAt).toBeTruthy();

    await agent.put(`/investigations/${id}/intent`).send({ intent: "Second version" }).expect(200);
    expect((await agent.get(`/investigations/${id}`)).body.investigation.intent).toBe("Second version");

    const cleared = await agent.put(`/investigations/${id}/intent`).send({ intent: "   " });
    expect(cleared.body.investigation.intent).toBeNull();
  });

  it("rejects invalid intent updates", async () => {
    const { body } = await agent.post("/investigations").send({ snapshotId });
    const id = body.investigation.id;
    expect((await agent.put(`/investigations/${id}/intent`).send({ intent: "x".repeat(4001) })).status).toBe(400);
    expect((await agent.put(`/investigations/${id}/intent`).send({ intent: 5 })).status).toBe(400);
    expect((await agent.put(`/investigations/${id}/intent`).send({})).status).toBe(400);
    expect((await agent.put(`/investigations/${randomUUID()}/intent`).send({ intent: "x" })).status).toBe(404);
    expect((await request(createApp()).put(`/investigations/${id}/intent`).send({ intent: "x" })).status).toBe(401);
  });

  describe("messages", () => {
    let id: string;
    const agentJobs = () =>
      pool.query("select status from jobs where investigation_id = $1 and kind = 'agent_reply'", [id]);

    beforeAll(async () => {
      // Finish whatever earlier tests left active, then start a fresh investigation.
      await pool.query(
        "update investigations set status = 'failed' where snapshot_id = $1 and status in ('queued', 'parsing')",
        [snapshotId],
      );
      const res = await agent.post("/investigations").send({ snapshotId, fresh: true }).expect(201);
      id = res.body.investigation.id;
    });

    it("refuses messages until the investigation is ready", async () => {
      const res = await agent.post(`/investigations/${id}/messages`).send({ content: "hello" });
      expect(res.status).toBe(409);
      expect(res.body.error).toBe("not_ready");
      expect((await agentJobs()).rowCount).toBe(0);
    });

    it("stores a message, queues one agent reply, and blocks a second until it is answered", async () => {
      await pool.query("update investigations set status = 'ready' where id = $1", [id]);

      const [a, b] = await Promise.all([
        agent.post(`/investigations/${id}/messages`).send({ content: "  What is exported to ISP-A?  " }),
        agent.post(`/investigations/${id}/messages`).send({ content: "second at the same moment" }),
      ]);
      expect([a.status, b.status].sort()).toEqual([201, 409]);
      const ok = a.status === 201 ? a : b;
      const blocked = a.status === 201 ? b : a;
      expect(blocked.body.error).toBe("agent_busy");
      expect(ok.body.message).toMatchObject({ role: "user", failed: false, createdBy: email, steps: [] });
      expect((await agentJobs()).rows).toEqual([{ status: "queued" }]);

      const pending = await agent.get(`/investigations/${id}/messages`);
      expect(pending.body.agentPending).toBe(true);
      expect(pending.body.agentProgress).toBeNull(); // queued, not yet picked up
      await pool.query(
        `update jobs set status = 'running', progress = '{"step": 3, "doing": "Batfish routes"}'
          where investigation_id = $1 and kind = 'agent_reply'`,
        [id],
      );
      const working = await agent.get(`/investigations/${id}/messages`);
      expect(working.body.agentProgress).toEqual({ step: 3, doing: "Batfish routes" });
      expect(pending.body.messages).toHaveLength(1);

      // What the worker does when it finishes: record the reply and close the job.
      await pool.query(
        `insert into messages (investigation_id, role, content, steps, suggestion)
         values ($1, 'agent', 'Only 192.0.2.0/24.', $2, $3)`,
        [
          id,
          JSON.stringify([{ tool: "search_configs", args: { pattern: "ISP-A" }, summary: "r1/frr.conf:7" }]),
          JSON.stringify({ mode: "verify", intent: "Only 192.0.2.0/24 is exported to ISP-A." }),
        ],
      );
      await pool.query("update jobs set status = 'done' where investigation_id = $1 and kind = 'agent_reply'", [id]);

      const done = await agent.get(`/investigations/${id}/messages`);
      expect(done.body.agentPending).toBe(false);
      expect(done.body.messages.map((m: { role: string }) => m.role)).toEqual(["user", "agent"]);
      expect(done.body.messages[1]).toMatchObject({ content: "Only 192.0.2.0/24.", createdBy: null });
      expect(done.body.messages[1].steps[0].tool).toBe("search_configs");
      expect(done.body.messages[0].suggestion).toBeNull();
      expect(done.body.messages[1].suggestion).toEqual({ mode: "verify", intent: "Only 192.0.2.0/24 is exported to ISP-A." });

      expect((await agent.post(`/investigations/${id}/messages`).send({ content: "and to ISP-B?" })).status).toBe(201);
    });

    it("rejects invalid messages and unknown investigations", async () => {
      const post = (target: string, body: unknown) => agent.post(`/investigations/${target}/messages`).send(body as object);
      expect((await post(id, { content: "   " })).status).toBe(400);
      expect((await post(id, { content: "x".repeat(4001) })).status).toBe(400);
      expect((await post(id, {})).status).toBe(400);
      expect((await post(randomUUID(), { content: "x" })).status).toBe(404);
      expect((await agent.get(`/investigations/${randomUUID()}/messages`)).status).toBe(404);
      expect((await request(createApp()).get(`/investigations/${id}/messages`)).status).toBe(401);
      expect((await request(createApp()).post(`/investigations/${id}/messages`).send({ content: "x" })).status).toBe(401);
    });
  });

  it("lets the starter delete an investigation, and nobody else except an admin", async () => {
    const { body } = await agent.post("/investigations").send({ snapshotId, fresh: true });
    const id = body.investigation.id;
    await pool.query("update investigations set status = 'ready' where id = $1", [id]);
    await agent.post(`/investigations/${id}/messages`).send({ content: "hello" }).expect(201);
    expect((await agent.get(`/snapshots/${snapshotId}`)).body.snapshot.resumeInvestigationId).toBe(id);

    const otherEmail = `test-${randomUUID()}@example.test`;
    await pool.query("insert into users (email, password_hash, role) values ($1, $2, 'requester')", [
      otherEmail,
      await hashPassword(password),
    ]);
    const other = request.agent(createApp());
    await other.post("/auth/login").send({ email: otherEmail, password }).expect(200);
    try {
      expect((await request(createApp()).delete(`/investigations/${id}`)).status).toBe(401);
      expect((await other.delete(`/investigations/${id}`)).status).toBe(403);
      expect((await other.get(`/snapshots/${snapshotId}`)).body.snapshot.resumeInvestigationId).toBeNull();
      await pool.query("update users set role = 'admin' where email = $1", [otherEmail]);
      expect((await agent.delete(`/investigations/${id}`)).status).toBe(204);
      expect((await agent.get(`/investigations/${id}`)).status).toBe(404);
      expect((await other.delete(`/investigations/${id}`)).status).toBe(404);
      const left = await pool.query(
        "select (select count(*) from messages where investigation_id = $1)::int as m, (select count(*) from jobs where investigation_id = $1)::int as j",
        [id],
      );
      expect(left.rows[0]).toEqual({ m: 0, j: 0 });
      expect((await agent.get(`/snapshots/${snapshotId}`)).status).toBe(200);
      expect((await agent.get(`/snapshots/${snapshotId}`)).body.snapshot.resumeInvestigationId).not.toBe(id);
    } finally {
      await pool.query("delete from sessions where user_id = (select id from users where email = $1)", [otherEmail]);
      await pool.query("delete from users where email = $1", [otherEmail]);
    }
  });

  it("returns 404 for unknown investigations", async () => {
    expect((await agent.get(`/investigations/${randomUUID()}`)).status).toBe(404);
    expect((await agent.get("/investigations/not-a-uuid")).status).toBe(404);
  });
});
