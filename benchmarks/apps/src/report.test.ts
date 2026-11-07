import { describe, expect, it } from "vitest";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { writeReport, type Controls } from "./report";

const rec = (taskId: string, run: number, o: Record<string, unknown> = {}) => ({
  builder: "claude-code", model: "m", taskId, run, status: "done", score: 0.9, passed: true, minutes: 10, minutesToRunnable: 8,
  dollars: 2, fixLoops: 1, journeys: [], probes: [], judge: 0.5, overRunCap: false, dir: "", tokensIn: 0, tokensOut: 0, ...o,
});
function results(recs: ReturnType<typeof rec>[], controls?: Partial<Controls>, suite?: { tasks: string[]; runs: number }) {
  const d = mkdtempSync(join(tmpdir(), "report-"));
  if (suite) writeFileSync(join(d, "suite.json"), JSON.stringify(suite));
  for (const r of recs) writeFileSync(join(d, `${r.taskId}-run${r.run}.json`), JSON.stringify(r));
  if (controls) writeFileSync(join(d, "controls.json"), JSON.stringify(controls));
  return d;
}
const good: Controls = { referencePassed: true, brokenFailedAllProbes: true, judgeOnReference: 0.95 };
const row = (md: string, id: string) => md.split("\n").find((l) => l.startsWith(`| ${id} `))!;

describe("report", () => {
  it("report_counts_pass_cubed_only_when_all_three_pass", () => {
    const d = results([
      rec("b01-todo", 1), rec("b01-todo", 2), rec("b01-todo", 3),
      rec("b02-slotline", 1), rec("b02-slotline", 2, { passed: false, score: 0.3 }), rec("b02-slotline", 3),
      rec("b03-curbside", 1), rec("b03-curbside", 2),
    ], good);
    const md = writeReport(d);
    expect(readFileSync(join(d, "report.md"), "utf8")).toBe(md);
    expect(row(md, "b01-todo")).toMatch(/\| yes \|/);
    expect(row(md, "b02-slotline")).toMatch(/\| no \|/);
    expect(row(md, "b03-curbside")).toMatch(/\| incomplete \(2\/3\) \|/); // only two runs, both passed
    expect(row(md, "b02-slotline")).toContain("0.70"); // mean of 0.9, 0.3, 0.9
    expect(md).toMatch(/pass\^3: 1 of 3 tasks/);
    expect(md).not.toMatch(/🔴|RED/);
  });

  it("judge_flags_itself_when_reference_scores_low", () => {
    const md = writeReport(results([rec("b01-todo", 1)], { ...good, judgeOnReference: 0.6 }));
    expect(md).toMatch(/RED: the judge scored the reference app 0\.60/);
    expect(row(md, "b01-todo")).toBeDefined(); // the judge is 15% and never decides pass/fail: scores still shown
  });

  it("withholds scores when a control failed", () => {
    const md = writeReport(results([rec("b01-todo", 1)], { ...good, brokenFailedAllProbes: false }));
    expect(md).toMatch(/RED: .*broken twin/);
    expect(md).toMatch(/withheld/);
    expect(row(md, "b01-todo")).toBeUndefined();
  });

  it("medians, fix loops and unknown-cost runs in the totals", () => {
    const md = writeReport(results([
      rec("b01-todo", 1, { minutes: 10, dollars: 1, fixLoops: 2 }),
      rec("b01-todo", 2, { minutes: 30, dollars: 3, fixLoops: 1 }),
      rec("b01-todo", 3, { minutes: 20, dollars: 0, costUnknown: true, fixLoops: 0 }),
    ], good));
    expect(row(md, "b01-todo")).toMatch(/\| 20\.0 \| \$2\.00 \| 3 \|/); // median minutes 20; median dollars over known costs (1, 3)
    expect(md).toMatch(/1 run with unknown cost/);
    expect(md).toMatch(/Spend: \$4\.00/);
  });

  it("shows subscription where billing is subscription", () => {
    const md = writeReport(results([rec("b01-todo", 1, { billing: "subscription", dollars: 0 }), rec("b01-todo", 2, { billing: "subscription", dollars: 0 }), rec("b01-todo", 3, { billing: "subscription", dollars: 0 })], good));
    expect(row(md, "b01-todo")).toMatch(/\| subscription \|/);
    expect(md).toMatch(/Spend: subscription/);
  });

  it("withholds scores when controls.json is missing, and flags a missing judge-on-reference score", () => {
    const md = writeReport(results([rec("b01-todo", 1)]));
    expect(md).toMatch(/controls\.json missing/);
    expect(md).toMatch(/withheld/);
    expect(row(md, "b01-todo")).toBeUndefined();
    const noJudge = writeReport(results([rec("b01-todo", 1)], { referencePassed: true, brokenFailedAllProbes: true }));
    expect(noJudge).toMatch(/RED: .*judge.*reference/);
  });

  it("uses the planned suite as the denominator: incomplete tasks and tasks that never ran", () => {
    const md = writeReport(results([
      rec("b01-todo", 1), rec("b01-todo", 2), rec("b01-todo", 3),
      rec("b02-slotline", 1), rec("b02-slotline", 2),
      rec("b03-curbside", 1, { passed: false }),
    ], good, { tasks: ["b01-todo", "b02-slotline", "b03-curbside", "b04-deskhero"], runs: 3 }));
    expect(row(md, "b01-todo")).toMatch(/\| yes \|/);
    expect(row(md, "b02-slotline")).toMatch(/\| incomplete \(2\/3\) \|/);
    expect(row(md, "b03-curbside")).toMatch(/\| no \|/);
    expect(md).toMatch(/pass\^3: 1 of 4 tasks/);
    expect(md).toMatch(/Never ran: b04-deskhero/);
  });

  it("notes runs that had no judge score", () => {
    const md = writeReport(results([rec("b01-todo", 1, { judgeFailed: true, judge: 0 }), rec("b01-todo", 2)], good));
    expect(md).toMatch(/1 run had no judge score \(counted as 0\)/);
  });
});
