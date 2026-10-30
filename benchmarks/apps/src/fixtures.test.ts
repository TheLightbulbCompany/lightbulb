import { describe, expect, it } from "vitest";
import { join } from "node:path";
import { withAppDir, root } from "./controls";
import { runProbes } from "./probes";
import { loadTask, playwrightJourneys } from "./runner";
import { serve } from "./serve";

// Each m1 fixture is the reference solution of its build task: serve it the way the controls do and it must pass every journey and probe.
// Serves four apps (Docker + pnpm + Chrome); run with EVAL_IT=1.
const FIXTURES: [fixture: string, task: string][] = [
  ["todo-m1", "b01-todo"],
  ["slotline-m1", "b02-slotline"],
  ["curbside-m1", "b03-curbside"],
  ["deskhero-m1", "b04-deskhero"],
];

describe.skipIf(!process.env.EVAL_IT)("m1 fixtures pass their build tasks", () => {
  for (const [fixture, taskId] of FIXTURES) {
    it(`${fixture} passes ${taskId}`, async () => {
      const task = loadTask(taskId);
      const probes = [...task.probes.filter((p) => p.kind !== "sql-injection"), ...task.probes.filter((p) => p.kind === "sql-injection")]; // sql-injection last: its payloads write to the db
      const r = await withAppDir(join(root, "fixtures", fixture), taskId, serve, async (url, dir, databaseUrl) => ({
        j: await playwrightJourneys(task, url),
        p: await runProbes(url, dir, probes, databaseUrl, task),
      }));
      if ("error" in r) throw new Error(`${fixture}: ${r.error}`);
      const lines = [...r.j.map((x) => `  journey ${x.id}: ${x.pass ? "PASS" : "FAIL"}`), ...r.p.map((x) => `  probe ${x.id}: ${x.pass ? "PASS" : "FAIL"} ${x.detail}`)];
      console.log(`${fixture} (${taskId})\n${lines.join("\n")}`);
      expect(r.j.length, lines.join("\n")).toBeGreaterThan(0);
      expect(r.j.every((x) => x.pass) && r.p.length === probes.length && r.p.every((x) => x.pass), lines.join("\n")).toBe(true);
    }, 25 * 60_000);
  }
});
