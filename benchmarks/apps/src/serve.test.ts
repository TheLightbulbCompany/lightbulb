import { describe, it, expect, afterEach } from "vitest";
import { execFileSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { serve } from "./serve.js";

const fx = (n: string) => resolve(import.meta.dirname, "__fixtures__", n);
const containerOn = (databaseUrl: string) =>
  execFileSync("docker", ["ps", "-aq", "--filter", `publish=${new URL(databaseUrl).port}`]).toString().trim();
const labelled = () =>
  execFileSync("docker", ["ps", "-q", "--filter", "label=lightbulb-eval=1"]).toString().split("\n").filter(Boolean).length;
const psql = (c: string, sql: string) =>
  execFileSync("docker", ["exec", c, "psql", "-U", "postgres", "-tAc", sql]).toString().trim();

describe.skipIf(!process.env.EVAL_IT)("serve", () => {
  const open: Array<{ stop(): Promise<void> }> = [];
  afterEach(async () => { await Promise.all(open.splice(0).map((s) => s.stop())); });
  const T = 600_000;

  it("unbuildable_app_scores_zero_and_keeps_log", async () => {
    const before = labelled();
    const s = await serve(fx("broken-next")); open.push(s);
    expect(s.built).toBe(false);
    expect(s.url).toBe("");
    expect(s.buildLog).toContain("error TS");
    // our container is gone (other tests' labelled containers may exist, so compare by count)
    expect(labelled()).toBeLessThanOrEqual(before);
  }, T);

  it("two_runs_get_isolated_databases", async () => {
    const [a, b] = await Promise.all([serve(fx("trivial-app")), serve(fx("trivial-app"))]); open.push(a, b);
    expect(a.built && b.built).toBe(true);
    expect(new URL(a.databaseUrl).port).not.toBe(new URL(b.databaseUrl).port);
    expect(await fetch(a.url)).toHaveProperty("ok", true);
    const [ca, cb] = [containerOn(a.databaseUrl), containerOn(b.databaseUrl)];
    psql(ca, "create table t(x int); insert into t values (1)");
    expect(psql(ca, "select count(*) from t")).toBe("1");
    expect(() => psql(cb, "select count(*) from t")).toThrow();
  }, T);

  it("before seeds the database ahead of the app's own install and migrate", async () => {
    const fixtureDir = mkdtempSync(join(tmpdir(), "fixture-")); // no drizzle.config.ts: nothing to migrate, seed only
    const s = await serve(fx("trivial-app"), { before: { fixtureDir, seedSql: "create table contacts(name text); insert into contacts values ('EVALMARK_1')" } }); open.push(s);
    expect(s.built).toBe(true);
    expect(psql(containerOn(s.databaseUrl), "select name from contacts")).toBe("EVALMARK_1");
    expect(s.buildLog.indexOf("seed")).toBeLessThan(s.buildLog.indexOf("pnpm install"));
    const bad = await serve(fx("trivial-app"), { before: { fixtureDir, seedSql: "insert into nope values (1)" } }); open.push(bad);
    expect(bad.built).toBe(false);
    expect(bad.buildLog).toMatch(/seed failed/);
  }, T);

  it("stop removes the container", async () => {
    const s = await serve(fx("trivial-app")); open.push(s);
    const c = containerOn(s.databaseUrl);
    expect(c).not.toBe("");
    await s.stop();
    await s.stop();
    expect(containerOn(s.databaseUrl)).toBe("");
    await expect(fetch(s.url)).rejects.toThrow();
  }, T);
});
