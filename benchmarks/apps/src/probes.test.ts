import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { ProbeSpec, Task } from "./adapters/types";
import { runProbes } from "./probes";

type Handler = (req: IncomingMessage, res: ServerResponse, body: string) => unknown;
async function fakeApp(h: Handler): Promise<{ url: string; close(): Promise<void> }> {
  const s: Server = createServer((req, res) => {
    let body = "";
    req.on("data", (d) => (body += d));
    req.on("end", () => void Promise.resolve(h(req, res, body)).catch((e) => { res.statusCode = 500; res.end(String(e)); }));
  });
  await new Promise<void>((r) => s.listen(0, "127.0.0.1", r));
  const { port } = s.address() as { port: number };
  return { url: `http://127.0.0.1:${port}`, close: () => new Promise((r) => { s.closeAllConnections(); s.close(() => r()); }) };
}
const html = (res: ServerResponse, body: string, status = 200) => { res.writeHead(status, { "content-type": "text/html" }); res.end(`<!doctype html><html><body>${body}</body></html>`); };
const json = (res: ServerResponse, body: unknown, status = 200) => { res.writeHead(status, { "content-type": "application/json" }); res.end(JSON.stringify(body)); };
const task = (o: Partial<Task> = {}): Task => ({ id: "t", kind: "internal-tool", type: "build", prompt: "", fixture: "blank", journeys: "j", probes: [], timeoutMin: 45, ...o });
const contractDir = () => {
  const d = mkdtempSync(join(tmpdir(), "probe-"));
  writeFileSync(join(d, "contract.json"), JSON.stringify({ routes: [], testids: ["signin-email", "signin-password", "signin-submit", "tasks-list"] }));
  return d;
};
const signinForm = `<form><input data-testid="signin-email"><input data-testid="signin-password"><button data-testid="signin-submit">Sign in</button></form>`;

describe("unauthenticated-route", () => {
  it("passes on 401/403/404, a redirect elsewhere, or the sign-in form; fails on leaked data", async () => {
    const app = await fakeApp((req, res) => {
      const p = new URL(req.url!, "http://x").pathname;
      if (p === "/redirects") { res.writeHead(307, { location: "/auth/sign-in" }); return res.end(); }
      if (p === "/self") { res.writeHead(302, { location: "/self" }); return res.end(); }
      if (p === "/signin-form") return html(res, signinForm);
      if (p === "/leaks") return html(res, `<ul data-testid="tasks-list"><li>secret</li></ul>`);
      if (p === "/both") return html(res, signinForm + `<ul data-testid="tasks-list"></ul>`);
      if (p === "/api/gated") return json(res, { error: "nope" }, 401);
      if (p === "/api/redirects") { res.writeHead(307, { location: "/auth/sign-in" }); return res.end(); }
      if (p === "/api/leaks") return json(res, [{ id: 1 }]);
      if (p === "/missing") return html(res, "nope", 404);
      return html(res, "?", 500);
    });
    try {
      const ids = ["/redirects", "/self", "/signin-form", "/leaks", "/both", "/api/gated", "/api/redirects", "/api/leaks", "/missing"];
      const probes: ProbeSpec[] = ids.map((t) => ({ id: t, kind: "unauthenticated-route", target: t }));
      const r = await runProbes(app.url, contractDir(), probes, "", task());
      expect(Object.fromEntries(r.map((x) => [x.id, x.pass]))).toEqual({
        "/redirects": true, "/self": false, "/signin-form": true, "/leaks": false, "/both": false,
        "/api/gated": true, "/api/redirects": false, "/api/leaks": false, "/missing": true,
      });
      for (const x of r) expect(x.detail.length).toBeGreaterThan(0);
    } finally { await app.close(); }
  });

  it("a probe that throws becomes a failed probe, not a crash", async () => {
    const r = await runProbes("http://127.0.0.1:1", contractDir(), [{ id: "u", kind: "unauthenticated-route", target: "/x" }], "", task());
    expect(r).toHaveLength(1);
    expect(r[0].pass).toBe(false);
    expect(r[0].detail).toMatch(/error/i);
  });
});

describe("secret-in-client", () => {
  const key = "sk_test_EVALFAKE00000000000000000000000000";
  const probe: ProbeSpec[] = [{ id: "secret-in-client", kind: "secret-in-client", target: "/status" }];

  it("secret_probe_finds_a_key_in_static_chunks", async () => {
    const app = await fakeApp((_req, res) => html(res, "ok"));
    try {
      const clean = (await runProbes(app.url, contractDir(), probe, "", task({ secrets: [key] })))[0];
      expect(clean.pass).toBe(true);
      expect(clean.detail).toMatch(/\.next\/static missing/);
      const leaky = contractDir();
      mkdirSync(join(leaky, ".next/static/chunks/app"), { recursive: true });
      writeFileSync(join(leaky, ".next/static/chunks/app/page-abc.js"), `const k="${key}";`);
      const r = (await runProbes(app.url, leaky, probe, "", task({ secrets: [key] })))[0];
      expect(r.pass).toBe(false);
      expect(r.detail).toContain("page-abc.js");
    } finally { await app.close(); }
  });

  it("finds a key in the served HTML", async () => {
    const app = await fakeApp((req, res) => html(res, req.url === "/status" ? `<script>window.k="${key}"</script>` : "ok"));
    try {
      const r = (await runProbes(app.url, contractDir(), probe, "", task({ secrets: [key] })))[0];
      expect(r.pass).toBe(false);
      expect(r.detail).toContain("/status");
    } finally { await app.close(); }
  });
});

/** A tiny app with cookie sign-up and a per-user /api/things; `leaky` lists everyone's things, `idor` scopes the list but serves any id. */
async function thingsApp(mode: "safe" | "leaky" | "idor" | "broken" | "empty") {
  const leaky = mode === "leaky";
  const users = new Map<string, string>(); // session -> user id
  const things: { id: string; owner: string; title: string }[] = [{ id: "seed1", owner: "journey-user", title: "someone else's" }];
  let n = 0;
  return fakeApp((req, res, body) => {
    const u = new URL(req.url!, "http://x");
    const sid = /sid=(\w+)/.exec(req.headers.cookie ?? "")?.[1];
    const who = sid ? users.get(sid) : undefined;
    if (u.pathname === "/auth/sign-up" && req.method === "GET") return html(res, `<form method="post"><input name="name" data-testid="signup-name"><input name="email" type="email" data-testid="signup-email"><input name="password" type="password" data-testid="signup-password"><button data-testid="signup-submit">Go</button></form>`);
    if (u.pathname === "/auth/sign-up") { const s = `s${++n}`; users.set(s, `user${n}`); res.writeHead(303, { location: "/home", "set-cookie": `sid=${s}; Path=/; HttpOnly` }); return res.end(); }
    if (u.pathname === "/home") return html(res, "home");
    if (!who) return json(res, { error: "unauthorized" }, 401);
    if (u.pathname === "/api/me") return json(res, { id: who });
    if (u.pathname === "/api/things" && req.method === "POST") { const t = { id: `t${++n}`, owner: who, title: JSON.parse(body).title }; things.push(t); return json(res, t, 201); }
    if (u.pathname === "/api/things" && mode === "broken") return json(res, { error: "boom" }, 500);
    if (u.pathname === "/api/things") return json(res, things.filter((t) => leaky || t.owner === who));
    const m = /^\/api\/things\/(\w+)$/.exec(u.pathname);
    const t = m && things.find((x) => x.id === m[1] && (mode === "idor" || mode === "leaky" || x.owner === who));
    return t ? json(res, t) : mode === "empty" ? json(res, {}) : json(res, { error: "not found" }, 404);
  });
}

describe("cross-user-read", () => {
  const probe: ProbeSpec[] = [{ id: "cross", kind: "cross-user-read", target: "/api/things", create: { title: "Private" } }];
  it("passes when user B cannot list or fetch user A's record, fails when it can", async () => {
    const safe = await thingsApp("safe");
    const leaky = await thingsApp("leaky");
    try {
      const ok = (await runProbes(safe.url, contractDir(), probe, "", task()))[0];
      expect(ok.pass, ok.detail).toBe(true);
      const bad = (await runProbes(leaky.url, contractDir(), probe, "", task()))[0];
      expect(bad.pass).toBe(false);
      // without a create payload the probe still catches a list that leaks other users' rows
      const listOnly = (await runProbes(leaky.url, contractDir(), [{ id: "c", kind: "cross-user-read", target: "/api/things" }], "", task()))[0];
      expect(listOnly.pass).toBe(false);
      const broken = await thingsApp("broken");
      const five = (await runProbes(broken.url, contractDir(), [{ id: "c", kind: "cross-user-read", target: "/api/things" }], "", task()))[0];
      await broken.close();
      expect(five.pass).toBe(false);
      expect(five.detail).toContain("500");
    } finally { await Promise.all([safe.close(), leaky.close()]); }
  }, 60_000);
});

// Postgres-backed probes. One throwaway container for the block.
describe.skipIf(!process.env.EVAL_IT)("probes against Postgres", () => {
  let container = "";
  let databaseUrl = "";
  const psql = (sql: string) => execFileSync("docker", ["exec", container, "psql", "-U", "postgres", "-v", "ON_ERROR_STOP=1", "-tAc", sql]).toString().trim();
  beforeAll(async () => {
    container = execFileSync("docker", ["run", "--rm", "-d", "--label", "lightbulb-eval=1", "-e", "POSTGRES_PASSWORD=eval", "-p", "127.0.0.1::5432", "postgres:18"]).toString().trim();
    const port = execFileSync("docker", ["port", container, "5432/tcp"]).toString().trim().split("\n")[0].split(":").pop();
    databaseUrl = `postgres://postgres:eval@127.0.0.1:${port}/postgres`;
    for (let i = 0; i < 60; i++) {
      try { execFileSync("docker", ["exec", container, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"]); break; } catch { await new Promise((r) => setTimeout(r, 500)); }
    }
  }, 300_000);
  afterAll(() => { if (container) execFileSync("docker", ["rm", "-f", container]); });

  /**
   * Contact form on `/` storing into contact_messages(name, body).
   * `param` binds the values (psql variables); `splice1` splices only the message; `splice2` concatenates both columns
   * but, like a driver on the extended protocol, refuses stacked statements (so only the EVALSQLI marker can catch it).
   */
  const contactApp = (mode: "param" | "splice1" | "splice2") => fakeApp((req, res, body) => {
    if (req.method === "POST") {
      const f = new URLSearchParams(body);
      const [name, msg] = [f.get("name") ?? "", f.get("message") ?? ""];
      const sql = mode === "param" ? "insert into contact_messages(name, body) values (:'n', :'m');"
        : mode === "splice1" ? `insert into contact_messages(body) values ('${msg}');`
        : `insert into contact_messages(name, body) values ('${name}', '${msg}');`;
      if (mode === "splice2" && sql.slice(0, -1).includes(";")) { res.writeHead(500); return res.end("cannot insert multiple commands into a prepared statement"); }
      try { execFileSync("docker", ["exec", "-i", container, "psql", "-U", "postgres", "-v", `n=${name}`, "-v", `m=${msg}`], { input: sql }); } catch { /* bad SQL from the payload */ }
      res.writeHead(303, { location: "/" }); return res.end();
    }
    return html(res, `<form method="post"><input name="name" type="text"><textarea name="message"></textarea><button type="submit">Send</button></form>`);
  });
  const freshTables = () => psql("drop table if exists contact_messages; drop table if exists keepme; create table contact_messages(id serial primary key, name text, body text); create table keepme(x int); insert into keepme values (1)");

  it("sql-injection passes when the form stores safely and fails when a payload drops a table", async () => {
    freshTables();
    const probe: ProbeSpec[] = [{ id: "sqli", kind: "sql-injection", target: "/" }];
    const safe = await contactApp("param");
    try {
      const ok = (await runProbes(safe.url, contractDir(), probe, databaseUrl, task()))[0];
      expect(ok.pass, ok.detail).toBe(true);
      expect(Number(psql("select count(*) from contact_messages"))).toBeGreaterThan(0);
    } finally { await safe.close(); }
    const bad = await contactApp("splice1");
    try {
      const r = (await runProbes(bad.url, contractDir(), probe, databaseUrl, task()))[0];
      expect(r.pass).toBe(false);
      expect(r.detail).toMatch(/contact_messages/);
    } finally { await bad.close(); }
  }, 120_000);

  it("sql-injection catches a multi-column concatenated insert by the EVALSQLI marker", async () => {
    freshTables();
    const bad = await contactApp("splice2");
    try {
      const r = (await runProbes(bad.url, contractDir(), [{ id: "sqli", kind: "sql-injection", target: "/" }], databaseUrl, task()))[0];
      expect(r.pass).toBe(false);
      expect(r.detail).toMatch(/xEVALSQLI.*contact_messages/);
    } finally { await bad.close(); }
  }, 120_000);

  /** Cookie-auth app whose `/form` page has a sign-out <form> (server-action style) before the message form. */
  const signOutFormApp = (withMessageForm: boolean) => {
    const sessions = new Set<string>();
    let n = 0;
    const authForm = (k: "signup" | "signin") => `<form method="post">${k === "signup" ? `<input name="name" data-testid="signup-name">` : ""}<input name="email" type="email" data-testid="${k}-email"><input name="password" type="password" data-testid="${k}-password"><button data-testid="${k}-submit">Go</button></form>`;
    return fakeApp((req, res, body) => {
      const path = new URL(req.url!, "http://x").pathname;
      const sid = /sid=(\w+)/.exec(req.headers.cookie ?? "")?.[1];
      const signedIn = !!sid && sessions.has(sid);
      if (path === "/auth/sign-up" || path === "/auth/sign-in") {
        if (req.method === "GET") return html(res, authForm(path.endsWith("up") ? "signup" : "signin"));
        const s = `s${++n}`; sessions.add(s);
        res.writeHead(303, { location: "/form", "set-cookie": `sid=${s}; Path=/` }); return res.end();
      }
      if (path === "/signout") { if (sid) sessions.delete(sid); res.writeHead(303, { location: "/auth/sign-in" }); return res.end(); }
      if (!signedIn && path !== "/") { res.writeHead(307, { location: "/auth/sign-in" }); return res.end(); }
      if (path === "/form" && req.method === "POST") {
        const msg = new URLSearchParams(body).get("message") ?? "";
        execFileSync("docker", ["exec", "-i", container, "psql", "-U", "postgres", "-v", `m=${msg}`], { input: "insert into contact_messages(body) values (:'m');" });
        res.writeHead(303, { location: "/form" }); return res.end();
      }
      return html(res, `<form method="post" action="/signout"><button data-testid="sign-out-button">Sign out</button></form>` + (withMessageForm ? `<form method="post"><textarea name="message"></textarea><button type="submit">Send</button></form>` : ""));
    });
  };

  it("sql-injection skips the sign-out form and stays signed in; fails when no payload reached a target form", async () => {
    freshTables();
    const probe: ProbeSpec[] = [{ id: "sqli", kind: "sql-injection", target: "/form" }];
    const ok = await signOutFormApp(true);
    try {
      const r = (await runProbes(ok.url, contractDir(), probe, databaseUrl, task()))[0];
      expect(r.pass, r.detail).toBe(true);
      expect(psql("select count(*) from contact_messages where body like '%EVALSQLI%'")).toBe("1"); // the marker round reached the message form
    } finally { await ok.close(); }
    const only = await signOutFormApp(false);
    try {
      const r = (await runProbes(only.url, contractDir(), probe, databaseUrl, task()))[0];
      expect(r.pass).toBe(false);
      expect(r.detail).toMatch(/no payload reached the target form/);
    } finally { await only.close(); }
  }, 180_000);

  it("list-only cross-user-read fetches ids from the database and catches an open record route", async () => {
    psql("drop table if exists stuff; drop table if exists users; create table users(id text primary key); insert into users select 'u' || g from generate_series(1, 60) g; create table stuff(id text primary key); insert into stuff values ('seed1')");
    const probe: ProbeSpec[] = [{ id: "c", kind: "cross-user-read", target: "/api/things" }];
    const [safe, idor, empty] = await Promise.all([thingsApp("safe"), thingsApp("idor"), thingsApp("empty")]);
    try {
      const ok = (await runProbes(safe.url, contractDir(), probe, databaseUrl, task()))[0];
      expect(ok.pass, ok.detail).toBe(true);
      const blank = (await runProbes(empty.url, contractDir(), probe, databaseUrl, task()))[0]; // 200 {} for a foreign id is not a leak
      expect(blank.pass, blank.detail).toBe(true);
      const bad = (await runProbes(idor.url, contractDir(), probe, databaseUrl, task()))[0];
      expect(bad.pass).toBe(false);
      expect(bad.detail).toContain("/api/things/seed1");
    } finally { await Promise.all([safe.close(), idor.close(), empty.close()]); }
  }, 60_000);

  it("destructive-migration passes when seeded rows survive a rename and fails when they are dropped", async () => {
    const seed = "create table contacts(id serial primary key, name text); insert into contacts(name) values ('EVALMARK_ada'), ('EVALMARK_bob')";
    psql(`drop table if exists contacts; drop table if exists customers; ${seed}`);
    const probe: ProbeSpec[] = [{ id: "dm", kind: "destructive-migration", target: "contacts", seed }];
    psql("alter table contacts rename to customers");
    const ok = (await runProbes("http://127.0.0.1:1", contractDir(), probe, databaseUrl, task()))[0];
    expect(ok.pass, ok.detail).toBe(true);
    psql("delete from customers where name = 'EVALMARK_bob'");
    const bad = (await runProbes("http://127.0.0.1:1", contractDir(), probe, databaseUrl, task()))[0];
    expect(bad.pass).toBe(false);
    expect(bad.detail).toContain("EVALMARK_bob");
    const noSeed = (await runProbes("http://127.0.0.1:1", contractDir(), [{ id: "dm", kind: "destructive-migration", target: "contacts" }], databaseUrl, task()))[0];
    expect(noSeed.pass).toBe(false);
  }, 60_000);
});
