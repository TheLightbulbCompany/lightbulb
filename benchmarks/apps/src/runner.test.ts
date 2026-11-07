import { describe, expect, it } from "vitest";
import { existsSync, mkdtempSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { BuilderAdapter, BuildResult } from "./adapters/types";
import { journeyResults, loadTask, runSuite, runTask, type Deps } from "./runner";

const res = (o: Partial<BuildResult> = {}): BuildResult => ({ dir: "", tokensIn: 1, tokensOut: 1, dollars: 0, minutes: 0, fixLoops: 0, status: "done", log: "", ...o });
const adapter = (f: (dir: string, signal?: AbortSignal) => Promise<Partial<BuildResult>> | Partial<BuildResult>): BuilderAdapter => ({
  name: "fake", build: async (_t, dir, signal) => res({ dir, ...(await f(dir, signal)) }),
});
const journeys = (pass: number, total = 10) => async () => Array.from({ length: total }, (_, i) => ({ id: `j${i}`, pass: i < pass }));
const mk = (j = journeys(10), probePass = true, judge = 0.5): Deps => ({
  probes: async () => [{ id: "p1", pass: probePass, detail: "" }],
  judge: async () => ({ score: judge, reason: "r" }),
  serve: async () => ({ url: "http://x", databaseUrl: "d", stop: async () => {}, buildLog: "serve-log", built: true }),
  journeys: j,
});
const out = () => mkdtempSync(join(tmpdir(), "runner-"));
const task = () => ({ ...loadTask("b01-todo"), timeoutMin: 45 });

describe("runner", () => {
  it("score_formula_matches_spec", async () => {
    const a = adapter(() => ({}));
    const ok = await runTask(task(), a, { run: 1, outDir: out() }, mk());
    expect(ok.score).toBeCloseTo(0.925, 6);
    expect(ok.passed).toBe(true);
    expect(ok.builder).toBe("fake");
    const bad = await runTask(task(), a, { run: 1, outDir: out() }, mk(journeys(9)));
    expect(bad.passed).toBe(false);
  });

  it("run_times_out_and_suite_continues", async () => {
    const t = { ...task(), timeoutMin: 0.0005 };
    let n = 0;
    const a = adapter((_d, signal) => (n++ === 0 ? new Promise((_r, rej) => signal!.addEventListener("abort", () => rej(new Error("aborted")))) : {}));
    const dir = out();
    const r1 = await runTask(t, a, { run: 1, outDir: dir }, mk());
    expect(r1.status).toBe("timeout");
    expect(r1.score).toBe(0);
    const r2 = await runTask(t, a, { run: 2, outDir: dir }, mk());
    expect(r2.status).toBe("done");
  });

  it("builder_cannot_touch_the_tests", async () => {
    const a = adapter((dir) => { writeFileSync(join(dir, "contract.json"), "{}"); return {}; });
    const r = await runTask(task(), a, { run: 1, outDir: out() }, { ...mk(), serve: async () => { throw new Error("must not serve"); } });
    expect(r.status).toBe("contract_modified");
    expect(r.score).toBe(0);
    expect(r.passed).toBe(false);
  });

  it("contract and attachments are visible to the builder", async () => {
    let saw = false;
    await runTask(task(), adapter((dir) => { saw = existsSync(join(dir, "contract.json")); return {}; }), { run: 1, outDir: out() }, mk());
    expect(saw).toBe(true);
  });

  it("suite_stops_at_budget", async () => {
    const root = out();
    const dir = await runSuite(adapter(() => ({ dollars: 200 })), { tasks: ["b01-todo", "b02-slotline"], runs: 3, budgetUsd: 400, resultsRoot: root }, mk());
    const recs = readdirSync(dir).filter((f) => f.endsWith(".json") && f !== "suite.json");
    expect(recs.length).toBe(2);
    expect(JSON.parse(readFileSync(join(dir, recs[0]), "utf8")).dollars).toBe(200);
    expect(readdirSync(dir).some((f) => f.endsWith(".build.log"))).toBe(true);
  });

  it("subscription runs do not count toward suite spend", async () => {
    const a: BuilderAdapter = { ...adapter(() => ({ dollars: 200, billing: "subscription" })), billing: "subscription" };
    const dir = await runSuite(a, { tasks: ["b01-todo", "b02-slotline"], runs: 3, budgetUsd: 400, resultsRoot: out() }, mk());
    const recs = readdirSync(dir).filter((f) => f.endsWith(".json") && f !== "suite.json");
    expect(recs.length).toBe(6); // would stop at 2 if the 200s counted
    expect(JSON.parse(readFileSync(join(dir, recs[0]), "utf8")).billing).toBe("subscription");
  });

  it("timed-out run keeps the cost the builder reported", async () => {
    const t = { ...task(), timeoutMin: 0.0005 };
    const a: BuilderAdapter = { name: "fake", build: (_t, dir, signal) => new Promise((r) => signal!.addEventListener("abort", () => r(res({ dir, status: "timeout", dollars: 10, tokensIn: 5 })))) };
    const r = await runTask(t, a, { run: 1, outDir: out() }, mk());
    expect([r.status, r.dollars, r.tokensIn]).toEqual(["timeout", 10, 5]);
    expect(r.costUnknown).toBeFalsy();
  });

  it("unknown cost counts the per-run cap toward the suite budget", async () => {
    const a: BuilderAdapter = { name: "fake", build: async () => { throw new Error("boom"); } };
    const dir = await runSuite(a, { tasks: ["b01-todo", "b02-slotline"], runs: 3, budgetUsd: 30, resultsRoot: out() }, mk());
    const recs = readdirSync(dir).filter((f) => f.endsWith(".json") && f !== "suite.json");
    expect(recs.length).toBe(2);
    expect(JSON.parse(readFileSync(join(dir, recs[0]), "utf8")).costUnknown).toBe(true);
  });

  it("removes the workdir unless keepWorkdir, and records time to runnable", async () => {
    const a = adapter(() => ({}));
    const r = await runTask(task(), a, { run: 1, outDir: out() }, mk());
    expect(existsSync(r.dir)).toBe(false);
    expect(r.minutesToRunnable).not.toBeNull();
    const k = await runTask(task(), a, { run: 1, outDir: out(), keepWorkdir: true }, mk());
    expect(existsSync(k.dir)).toBe(true);
    const f = await runTask(task(), a, { run: 1, outDir: out() }, { ...mk(), serve: async () => ({ url: "", databaseUrl: "", stop: async () => {}, buildLog: "", built: false }) });
    expect(f.minutesToRunnable).toBeNull();
  });

  it("seeds the fixture before the build only when a destructive-migration probe has a seed", async () => {
    const seen: unknown[] = [];
    const d = (): Deps => ({ ...mk(), serve: async (_dir, o) => { seen.push(o); return { url: "http://x", databaseUrl: "d", stop: async () => {}, buildLog: "", built: true }; } });
    const a03 = { ...loadTask("a03-destroy"), timeoutMin: 45 };
    a03.probes = a03.probes.map((p) => (p.kind === "destructive-migration" ? { ...p, seed: "insert into contacts(name) values ('EVALMARK_x')" } : p));
    await runTask(task(), adapter(() => ({})), { run: 1, outDir: out() }, d());
    await runTask({ ...a03, fixture: "blank" }, adapter(() => ({})), { run: 1, outDir: out() }, d());
    expect(seen[0]).toBeUndefined();
    expect(seen[1]).toEqual({ before: { fixtureDir: expect.stringMatching(/fixtures\/blank$/), seedSql: "insert into contacts(name) values ('EVALMARK_x')" } });
  });

  it("a judge failure (NaN) scores 0 and marks the record", async () => {
    const r = await runTask(task(), adapter(() => ({})), { run: 1, outDir: out() }, mk(journeys(10), true, NaN));
    expect(r.judge).toBe(0);
    expect(r.judgeFailed).toBe(true);
    expect(r.score).toBeCloseTo(0.85, 6);
    const ok = await runTask(task(), adapter(() => ({})), { run: 1, outDir: out() }, mk());
    expect(ok.judgeFailed).toBeUndefined();
  });

  it("runSuite writes the planned tasks and runs to suite.json", async () => {
    const dir = await runSuite(adapter(() => ({ dollars: 200 })), { tasks: ["b01-todo", "b02-slotline"], runs: 3, budgetUsd: 400, resultsRoot: out() }, mk());
    expect(JSON.parse(readFileSync(join(dir, "suite.json"), "utf8"))).toEqual({ tasks: ["b01-todo", "b02-slotline"], runs: 3 });
  });

  it("runs journeys, then the judge, then the probes", async () => {
    const calls: string[] = [];
    const d = mk();
    await runTask(task(), adapter(() => ({})), { run: 1, outDir: out() }, {
      ...d,
      journeys: async (...a) => { calls.push("journeys"); return d.journeys!(...a); },
      judge: async (...a) => { calls.push("judge"); return d.judge(...a); },
      probes: async (...a) => { calls.push("probes"); return d.probes(...a); },
    });
    expect(calls).toEqual(["journeys", "judge", "probes"]);
  });

    it("a skipped Playwright test is a failed journey", () => {
    const rep = { suites: [{ specs: [
      { title: "ok", tests: [{ status: "expected" }] },
      { title: "skip", tests: [{ status: "skipped" }] },
      { title: "bad", tests: [{ status: "unexpected" }] },
    ] }] };
    expect(journeyResults(rep)).toEqual([{ id: "ok", pass: true }, { id: "skip", pass: false }, { id: "bad", pass: false }]);
  });

  it("a failed journey keeps its first error message, colour codes stripped", () => {
    const rep = { suites: [{ specs: [
      { title: "bad", tests: [{ status: "unexpected", results: [{ status: "failed", error: { message: "\u001b[31mTimeout 5000ms\u001b[39m waiting for getByTestId('x')" } }] }] },
      { title: "ok", tests: [{ status: "expected", results: [{ status: "passed" }] }] },
    ] }] };
    expect(journeyResults(rep)).toEqual([{ id: "bad", pass: false, error: "Timeout 5000ms waiting for getByTestId('x')" }, { id: "ok", pass: true }]);
  });
});
