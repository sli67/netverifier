import { randomUUID } from "node:crypto";
import request from "supertest";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { createApp } from "../src/app.js";
import { hashPassword } from "../src/auth/passwords.js";
import { pool } from "../src/db.js";

const password = "correct horse battery staple";
const emails: string[] = [];
const HASH = "a".repeat(64);

async function signIn(role: string) {
  const email = `test-${randomUUID()}@example.test`;
  emails.push(email);
  await pool.query("insert into users (email, password_hash, role) values ($1, $2, $3)", [
    email,
    await hashPassword(password),
    role,
  ]);
  const agent = request.agent(createApp());
  await agent.post("/auth/login").send({ email, password }).expect(200);
  return agent;
}

let requester: Awaited<ReturnType<typeof signIn>>;
let approver: Awaited<ReturnType<typeof signIn>>;
let snapshotId: string;
let investigationId: string;

/** A fresh investigation, ready and with an intent unless told otherwise. */
async function investigation(intent: string | null = "Block guests from 10.0.0.0/8", status = "ready") {
  const res = await requester.post("/investigations").send({ snapshotId, fresh: true });
  const id = res.body.investigation.id;
  await pool.query("update investigations set status = $2, intent = $3 where id = $1", [id, status, intent]);
  return id as string;
}

/** Puts a run where the worker would leave it after validating a candidate. */
async function awaitingApproval(runId: string) {
  const candidate = await pool.query(
    `insert into candidates (run_id, round, content_hash, s3_prefix, files, diff, summary)
     values ($1, 1, $2, 'runs/x/candidates/1/', '[]', 'diff', 'summary') returning id`,
    [runId, HASH],
  );
  await pool.query(
    `update runs set status = 'waiting_approval', state = $2 where id = $1`,
    [runId, JSON.stringify({ attempts: [{ round: 1, passed: true, content_hash: HASH }] })],
  );
  await pool.query("update jobs set status = 'done' where run_id = $1", [runId]);
  return candidate.rows[0].id as string;
}

const jobsFor = async (runId: string) =>
  (await pool.query("select payload, status from jobs where run_id = $1 order by created_at", [runId])).rows;

beforeAll(async () => {
  requester = await signIn("requester");
  approver = await signIn("approver");
  const res = await requester
    .post("/snapshots")
    .send({ name: "for runs", files: [{ path: "edge.cfg", content: "hostname edge\n" }] })
    .expect(201);
  snapshotId = res.body.snapshot.id;
  investigationId = await investigation();
});

afterAll(async () => {
  await requester.delete(`/snapshots/${snapshotId}`).expect(204);
  await pool.query("delete from sessions where user_id in (select id from users where email = any($1))", [emails]);
  await pool.query("delete from users where email = any($1)", [emails]);
  await pool.end();
});

describe("runs", () => {
  it("requires authentication", async () => {
    const app = createApp();
    expect((await request(app).post("/runs").send({ investigationId, mode: "verify" })).status).toBe(401);
    expect((await request(app).get("/runs").query({ investigationId })).status).toBe(401);
  });

  it("refuses to start without a ready investigation and an intent", async () => {
    expect((await requester.post("/runs").send({ investigationId: randomUUID(), mode: "verify" })).status).toBe(404);
    expect((await requester.post("/runs").send({ investigationId, mode: "deploy" })).status).toBe(400);
    const parsing = await investigation("an intent", "parsing");
    expect((await requester.post("/runs").send({ investigationId: parsing, mode: "verify" })).body.error).toBe("not_ready");
    const blank = await investigation(null);
    expect((await requester.post("/runs").send({ investigationId: blank, mode: "verify" })).body.error).toBe("no_intent");
  });

  it("starts one run with the intent as it was, and queues its job", async () => {
    const [a, b] = await Promise.all([
      requester.post("/runs").send({ investigationId, mode: "change" }),
      requester.post("/runs").send({ investigationId, mode: "change" }),
    ]);
    expect([a.status, b.status].sort()).toEqual([201, 409]);
    const run = (a.status === 201 ? a : b).body.run;
    expect((a.status === 409 ? a : b).body.error).toBe("run_active");
    expect(run).toMatchObject({ mode: "change", status: "queued", intent: "Block guests from 10.0.0.0/8", state: {} });
    expect(await jobsFor(run.id)).toEqual([{ payload: { action: "start" }, status: "queued" }]);

    await pool.query("update investigations set intent = 'changed later' where id = $1", [investigationId]);
    expect((await requester.get(`/runs/${run.id}`)).body.run.intent).toBe("Block guests from 10.0.0.0/8");

    const list = await requester.get("/runs").query({ investigationId });
    expect(list.body.runs.map((r: { id: string }) => r.id)).toEqual([run.id]);
    expect(list.body.runs[0].state).toBeUndefined();

    // Once it ends, another can start.
    expect((await requester.post(`/runs/${run.id}/cancel`)).body.run.status).toBe("cancelled");
    expect((await requester.post(`/runs/${run.id}/cancel`)).body.error).toBe("not_active");
    expect((await requester.post("/runs").send({ investigationId, mode: "verify" })).status).toBe(201);
  });

  it("starts an audit of a named structure without needing an intent", async () => {
    const inv = await investigation(null);
    expect((await requester.post("/runs").send({ investigationId: inv, mode: "audit" })).body.error).toBe("target_required");
    const misplaced = await requester.post("/runs").send({ investigationId: inv, mode: "verify", target: "ISP-A-OUT" });
    expect(misplaced.body.error).toBe("target_only_for_audit");

    const started = await requester.post("/runs").send({ investigationId: inv, mode: "audit", target: " ISP-A-OUT " });
    expect(started.status).toBe(201);
    expect(started.body.run).toMatchObject({ mode: "audit", target: "ISP-A-OUT", intent: "Audit ISP-A-OUT" });
    // The investigation's own intent is left alone.
    expect((await pool.query("select intent from investigations where id = $1", [inv])).rows[0].intent).toBeNull();
    await requester.post(`/runs/${started.body.run.id}/cancel`).expect(200);
  });

  it("saves an intent given with the run, but not when the run cannot start", async () => {
    const inv = await investigation(null);
    const started = await requester
      .post("/runs")
      .send({ investigationId: inv, mode: "verify", intent: "  Guests cannot reach 10.0.0.0/8. " });
    expect(started.status).toBe(201);
    expect(started.body.run.intent).toBe("Guests cannot reach 10.0.0.0/8.");
    const saved = await pool.query("select intent, intent_updated_by from investigations where id = $1", [inv]);
    expect(saved.rows[0].intent).toBe("Guests cannot reach 10.0.0.0/8.");

    // A run is already active: the new intent is not saved either.
    const refused = await requester.post("/runs").send({ investigationId: inv, mode: "change", intent: "Something else." });
    expect(refused.body.error).toBe("run_active");
    const kept = await pool.query("select intent from investigations where id = $1", [inv]);
    expect(kept.rows[0].intent).toBe("Guests cannot reach 10.0.0.0/8.");
    expect((await requester.post("/runs").send({ investigationId: inv, mode: "change", intent: " " })).status).toBe(400);
  });

  it("links a run started from a chat suggestion to that message, and shows it in the conversation", async () => {
    const inv = await investigation();
    const other = await investigation();
    const insert = (target: string) =>
      pool.query<{ id: string }>(
        `insert into messages (investigation_id, role, content, steps, route, duration_ms, model, suggestion)
         values ($1, 'agent', 'GUEST permits everything first.', '[]', 'change', 4200, 'deepseek-flash', $2) returning id`,
        [target, JSON.stringify({ mode: "change", intent: "Guests cannot reach 10.0.0.0/8." })],
      );
    const message = (await insert(inv)).rows[0]!.id;
    const foreign = (await insert(other)).rows[0]!.id;

    const wrong = await requester.post("/runs").send({ investigationId: inv, mode: "change", messageId: foreign });
    expect([wrong.status, wrong.body.error]).toEqual([400, "message_not_found"]);

    const started = await requester
      .post("/runs")
      .send({ investigationId: inv, mode: "change", intent: "Guests cannot reach 10.0.0.0/8.", messageId: message });
    expect(started.status).toBe(201);
    expect(started.body.run.messageId).toBe(message);

    const { body } = await requester.get(`/investigations/${inv}/messages`);
    expect(body.messages[0]).toMatchObject({ route: "change", durationMs: 4200, model: "deepseek-flash" });
    expect(body.messages[0].runs).toEqual([
      { id: started.body.run.id, mode: "change", status: "queued", outcome: null, createdAt: expect.any(String) },
    ]);
    // Deleting the investigation removes the message and the run together.
    await requester.delete(`/investigations/${inv}`).expect(204);
  });

  it("accepts an answer only while the run is waiting for one, and only from its starter", async () => {
    const inv = await investigation();
    const { body } = await requester.post("/runs").send({ investigationId: inv, mode: "verify" });
    const id = body.run.id;
    expect((await requester.post(`/runs/${id}/answer`).send({ answer: "the edge one" })).body.error).toBe("not_waiting");

    await pool.query("update runs set status = 'waiting_user' where id = $1", [id]);
    expect((await approver.post(`/runs/${id}/answer`).send({ answer: "x" })).status).toBe(403);
    expect((await requester.post(`/runs/${id}/answer`).send({ answer: "  " })).status).toBe(400);

    const answered = await requester.post(`/runs/${id}/answer`).send({ answer: "the edge one" });
    expect(answered.body.run.status).toBe("running");
    expect((await jobsFor(id)).at(-1)).toEqual({ payload: { action: "answer", answer: "the edge one" }, status: "queued" });
    // A second answer has nothing to answer.
    expect((await requester.post(`/runs/${id}/answer`).send({ answer: "again" })).body.error).toBe("not_waiting");
  });

  it("takes the reviewed checks only at the review step, from the starter, for checks that exist", async () => {
    const inv = await investigation();
    const { body } = await requester.post("/runs").send({ investigationId: inv, mode: "change" });
    const id = body.run.id;
    const review = (who: typeof requester, sent: object) => who.post(`/runs/${id}/checks`).send(sent);
    expect((await review(requester, { drop: [] })).body.error).toBe("not_waiting");

    // Waiting on a question is not waiting on the checks, and the reverse.
    await pool.query("update runs set status = 'waiting_user', stage = 'clarify' where id = $1", [id]);
    expect((await review(requester, { drop: [] })).body.error).toBe("not_waiting");
    const checks = [{ requirement: "R1" }, { requirement: "R2" }];
    await pool.query("update runs set stage = 'review_checks', state = $2 where id = $1", [id, JSON.stringify({ checks })]);
    expect((await requester.post(`/runs/${id}/answer`).send({ answer: "x" })).body.error).toBe("not_waiting");

    expect((await review(approver, { drop: [] })).status).toBe(403);
    expect((await review(requester, { drop: [2] })).body.error).toBe("unknown_check");
    expect((await review(requester, { drop: [-1] })).status).toBe(400);

    const sent = await review(requester, { drop: [1, 1], note: "  R1 should only cover tagged routes. " });
    expect(sent.body.run.status).toBe("running");
    expect((await jobsFor(id)).at(-1)).toEqual({
      payload: { action: "checks", drop: [1], note: "R1 should only cover tagged routes.", by: emails[0] },
      status: "queued",
    });
    expect((await review(requester, { drop: [] })).body.error).toBe("not_waiting");
  });

  it("records a decision against the validated candidate's hash", async () => {
    const inv = await investigation();
    const { body } = await requester.post("/runs").send({ investigationId: inv, mode: "change" });
    const id = body.run.id;
    const decide = (who: typeof requester, extra: object = {}) =>
      who.post(`/runs/${id}/decision`).send({ decision: "approved", candidateHash: HASH, ...extra });

    expect((await decide(approver)).body.error).toBe("not_waiting");
    const candidateId = await awaitingApproval(id);

    // The requester cannot approve, and neither can a requester-role colleague.
    expect((await decide(requester)).status).toBe(403);
    expect((await decide(approver, { candidateHash: "b".repeat(64) })).body.error).toBe("candidate_mismatch");
    expect((await decide(approver, { candidateHash: "nope" })).status).toBe(400);
    expect((await pool.query("select 1 from approvals where run_id = $1", [id])).rowCount).toBe(0);

    const decided = await decide(approver, { comment: "Looks right." });
    expect(decided.status).toBe(200);
    expect(decided.body.run).toMatchObject({
      status: "running",
      approval: { decision: "approved", candidateHash: HASH, comment: "Looks right.", decidedBy: emails[1] },
    });
    const approval = await pool.query("select candidate_id, candidate_hash from approvals where run_id = $1", [id]);
    expect(approval.rows).toEqual([{ candidate_id: candidateId, candidate_hash: HASH }]);
    expect((await jobsFor(id)).at(-1)!.payload).toEqual({
      action: "decision",
      decision: "approved",
      candidate_hash: HASH,
      by: emails[1],
    });
    expect((await decide(approver)).body.error).toBe("not_waiting");
  });

  it("does not let an approver approve their own run, and refuses a stale candidate", async () => {
    const res = await approver.post("/investigations").send({ snapshotId, fresh: true });
    const inv = res.body.investigation.id;
    await pool.query("update investigations set status = 'ready', intent = 'x' where id = $1", [inv]);
    const { body } = await approver.post("/runs").send({ investigationId: inv, mode: "change" });
    await awaitingApproval(body.run.id);
    const own = await approver.post(`/runs/${body.run.id}/decision`).send({ decision: "approved", candidateHash: HASH });
    expect([own.status, own.body.error]).toEqual([403, "own_run"]);

    // The last attempt did not pass validation: its hash cannot be approved.
    await pool.query("update runs set state = $2 where id = $1", [
      body.run.id,
      JSON.stringify({ attempts: [{ round: 1, passed: false, content_hash: HASH }] }),
    ]);
    const admin = await signIn("admin");
    const stale = await admin.post(`/runs/${body.run.id}/decision`).send({ decision: "approved", candidateHash: HASH });
    expect(stale.body.error).toBe("candidate_mismatch");
    await approver.delete(`/investigations/${inv}`).expect(204);
  });

  it("deploys only an approved candidate, one deployment per lab at a time", async () => {
    const inv = await investigation();
    const { body } = await requester.post("/runs").send({ investigationId: inv, mode: "change" });
    const id = body.run.id;
    await awaitingApproval(id);
    expect((await approver.post(`/runs/${id}/deploy`)).body.error).toBe("not_approved");
    await approver.post(`/runs/${id}/decision`).send({ decision: "approved", candidateHash: HASH }).expect(200);

    expect((await requester.post(`/runs/${id}/deploy`)).status).toBe(403);
    const started = await approver.post(`/runs/${id}/deploy`);
    expect(started.status).toBe(201);
    expect(started.body.run.deployments).toMatchObject([{ status: "queued", candidateHash: HASH, createdBy: emails[1] }]);
    const job = await pool.query(
      "select kind, max_attempts, deployment_id from jobs where run_id = $1 and kind = 'deploy_change'",
      [id],
    );
    expect(job.rows).toEqual([{ kind: "deploy_change", max_attempts: 2, deployment_id: started.body.run.deployments[0].id }]);
    expect((await approver.post(`/runs/${id}/deploy`)).body.error).toBe("deployment_active");

    // Deleting the investigation removes the deployment too.
    await requester.delete(`/investigations/${inv}`).expect(204);
    expect((await pool.query("select 1 from deployments where run_id = $1", [id])).rowCount).toBe(0);
  });

  it("removes runs, candidates and approvals with their investigation", async () => {
    const inv = await investigation();
    const { body } = await requester.post("/runs").send({ investigationId: inv, mode: "change" });
    await awaitingApproval(body.run.id);
    await approver.post(`/runs/${body.run.id}/decision`).send({ decision: "rejected", candidateHash: HASH }).expect(200);

    await requester.delete(`/investigations/${inv}`).expect(204);
    const left = await pool.query(
      `select (select count(*) from runs where id = $1)::int as runs,
              (select count(*) from candidates where run_id = $1)::int as candidates,
              (select count(*) from approvals where run_id = $1)::int as approvals,
              (select count(*) from jobs where run_id = $1)::int as jobs`,
      [body.run.id],
    );
    expect(left.rows[0]).toEqual({ runs: 0, candidates: 0, approvals: 0, jobs: 0 });
    expect((await requester.get(`/runs/${body.run.id}`)).status).toBe(404);
  });

  it("returns 404 for unknown runs", async () => {
    expect((await requester.get(`/runs/${randomUUID()}`)).status).toBe(404);
    expect((await requester.get("/runs/nope")).status).toBe(404);
    expect((await requester.post(`/runs/${randomUUID()}/answer`).send({ answer: "x" })).status).toBe(404);
    expect((await requester.post(`/runs/${randomUUID()}/cancel`)).status).toBe(404);
  });
});
