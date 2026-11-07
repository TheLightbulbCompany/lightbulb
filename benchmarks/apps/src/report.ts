import { existsSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { RunRecord } from "./adapters/types";

/** Written by the controls (Task 3) into the suite's results dir. */
export interface Controls { referencePassed: boolean; brokenFailedAllProbes: boolean; judgeOnReference: number }

const median = (xs: number[]) => {
  if (!xs.length) return NaN;
  const s = [...xs].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};
const sub = (r: RunRecord) => r.billing === "subscription";
const usd = (x: number) => (Number.isNaN(x) ? "unknown" : `$${x.toFixed(2)}`);

/** Summarise a results dir into report.md (also returned). */
export function writeReport(resultsDir: string): string {
  const recs: RunRecord[] = readdirSync(resultsDir)
    .filter((f) => f.endsWith(".json") && f !== "controls.json")
    .map((f) => JSON.parse(readFileSync(join(resultsDir, f), "utf8")))
    .filter((r) => r.taskId);
  const cf = join(resultsDir, "controls.json");
  const controls: Controls | null = existsSync(cf) ? JSON.parse(readFileSync(cf, "utf8")) : null;

  const sf = join(resultsDir, "suite.json");
  const suite: { tasks: string[]; runs: number } = existsSync(sf) ? JSON.parse(readFileSync(sf, "utf8")) : { tasks: [...new Set(recs.map((r) => r.taskId))], runs: 3 };

  const lines = ["# Apps eval report", "", `Builder: ${recs[0]?.builder ?? "?"} · model: ${recs[0]?.model ?? "?"} · ${recs.length} runs`, ""];
  if (!controls) {
    lines.push("**RED: controls.json missing: the sanity controls were not recorded for this suite.**", "", "Scores withheld: without passing controls the suite cannot be trusted to measure builders.");
    return save(resultsDir, lines);
  }
  if (!controls.referencePassed) lines.push("**RED: the reference app did not score 100% on its task.**", "");
  if (!controls.brokenFailedAllProbes) lines.push("**RED: the broken twin passed at least one security probe.**", "");
  if (typeof controls.judgeOnReference !== "number" || !(controls.judgeOnReference >= 0.9))
    lines.push(`**RED: the judge scored the reference app ${typeof controls.judgeOnReference === "number" ? controls.judgeOnReference.toFixed(2) : "(no score recorded)"} (below 0.90); judge scores are suspect.**`, "");
  if (!controls.referencePassed || !controls.brokenFailedAllProbes) {
    lines.push("Scores withheld: a sanity control failed, so the suite cannot be trusted to measure builders.");
    return save(resultsDir, lines);
  }

  const byTask = new Map<string, RunRecord[]>();
  for (const r of recs) byTask.set(r.taskId, [...(byTask.get(r.taskId) ?? []), r]);
  lines.push("| task | pass^3 | mean score | median minutes | median dollars | fix loops |", "|---|---|---|---|---|---|");
  let cubed = 0;
  for (const [id, rs] of [...byTask].sort(([a], [b]) => a.localeCompare(b))) {
    // pass^3: every planned run ran and passed; fewer runs (budget stop) is "incomplete", not "no"
    const p3 = rs.some((r) => !r.passed) ? "no" : rs.length >= suite.runs ? "yes" : `incomplete (${rs.length}/${suite.runs})`;
    if (p3 === "yes") cubed++;
    const mean = rs.reduce((a, r) => a + r.score, 0) / rs.length;
    lines.push(`| ${id} | ${p3} | ${mean.toFixed(2)} | ${median(rs.map((r) => r.minutes)).toFixed(1)} | ${rs.every(sub) ? "subscription" : usd(median(rs.filter((r) => !r.costUnknown && !sub(r)).map((r) => r.dollars)))} | ${rs.reduce((a, r) => a + r.fixLoops, 0)} |`);
  }
  const unknown = recs.filter((r) => r.costUnknown).length;
  const subs = recs.filter(sub).length;
  const spend = recs.filter((r) => !r.costUnknown && !sub(r)).reduce((a, r) => a + r.dollars, 0);
  const noJudge = recs.filter((r) => r.judgeFailed).length;
  const never = suite.tasks.filter((t) => !byTask.has(t));
  const meanAll = recs.length ? recs.reduce((a, r) => a + r.score, 0) / recs.length : 0;
  lines.push("", "## Totals", "",
    `- pass^3: ${cubed} of ${suite.tasks.length} tasks`,
    ...(never.length ? [`- Never ran: ${never.join(", ")}`] : []),
    `- Mean score: ${meanAll.toFixed(2)} over ${recs.length} runs`,
    `- Median minutes per run: ${median(recs.map((r) => r.minutes)).toFixed(1)}`,
    `- Spend: ${subs === recs.length && subs ? "subscription (no per-run dollars)" : usd(spend) + (subs ? ` plus ${subs} subscription run${subs > 1 ? "s" : ""}` : "")}${unknown ? ` (plus ${unknown} run${unknown > 1 ? "s" : ""} with unknown cost, not included)` : ""}`,
    `- Fix loops: ${recs.reduce((a, r) => a + r.fixLoops, 0)}`,
    ...(noJudge ? [`- ${noJudge} run${noJudge > 1 ? "s" : ""} had no judge score (counted as 0)`] : []));
  return save(resultsDir, lines);
}

function save(dir: string, lines: string[]): string {
  const md = lines.join("\n") + "\n";
  writeFileSync(join(dir, "report.md"), md);
  return md;
}
