import { randomUUID } from "node:crypto";
import request from "supertest";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { createApp } from "../src/app.js";
import { hashPassword } from "../src/auth/passwords.js";
import { pool } from "../src/db.js";

const email = `test-${randomUUID()}@example.test`;
const password = "correct horse battery staple";

beforeAll(async () => {
  await pool.query("insert into users (email, password_hash, role) values ($1, $2, 'approver')", [
    email,
    await hashPassword(password),
  ]);
});

afterAll(async () => {
  await pool.query("delete from users where email = $1", [email]);
  await pool.end();
});

describe("auth", () => {
  it("rejects unauthenticated /auth/me", async () => {
    const res = await request(createApp()).get("/auth/me");
    expect(res.status).toBe(401);
  });

  it("rejects a wrong password and an unknown email identically", async () => {
    const app = createApp();
    const wrong = await request(app).post("/auth/login").send({ email, password: "nope" });
    const unknown = await request(app)
      .post("/auth/login")
      .send({ email: "nobody@example.test", password });
    expect(wrong.status).toBe(401);
    expect(unknown.status).toBe(401);
    expect(wrong.body).toEqual(unknown.body);
    expect(wrong.headers["set-cookie"]).toBeUndefined();
  });

  it("rejects a malformed body", async () => {
    const res = await request(createApp()).post("/auth/login").send({ email });
    expect(res.status).toBe(400);
  });

  it("logs in, serves /auth/me, and logs out", async () => {
    const agent = request.agent(createApp());

    const login = await agent.post("/auth/login").send({ email: email.toUpperCase(), password });
    expect(login.status).toBe(200);
    expect(login.body.user).toMatchObject({ email, role: "approver" });
    expect(login.body.user.password_hash).toBeUndefined();
    const cookie = String(login.headers["set-cookie"]);
    expect(cookie).toMatch(/HttpOnly/);
    expect(cookie).toMatch(/SameSite=Lax/);

    const me = await agent.get("/auth/me");
    expect(me.status).toBe(200);
    expect(me.body.user.email).toBe(email);

    const token = /sid=([^;]+)/.exec(cookie)?.[1];
    expect((await agent.post("/auth/logout")).status).toBe(204);
    expect((await agent.get("/auth/me")).status).toBe(401);

    // The old token must be dead server-side, not just cleared in the browser.
    const replay = await request(createApp()).get("/auth/me").set("Cookie", `sid=${token}`);
    expect(replay.status).toBe(401);
  });

  it("rejects a disabled user's existing session", async () => {
    const agent = request.agent(createApp());
    await agent.post("/auth/login").send({ email, password });
    await pool.query("update users set disabled_at = now() where email = $1", [email]);
    try {
      expect((await agent.get("/auth/me")).status).toBe(401);
      const login = await agent.post("/auth/login").send({ email, password });
      expect(login.status).toBe(401);
    } finally {
      await pool.query("update users set disabled_at = null where email = $1", [email]);
    }
  });

  it("rate limits repeated login attempts", async () => {
    const app = createApp();
    let last = 0;
    for (let i = 0; i < 11; i++) {
      last = (await request(app).post("/auth/login").send({ email, password: "nope" })).status;
    }
    expect(last).toBe(429);
  });
});
