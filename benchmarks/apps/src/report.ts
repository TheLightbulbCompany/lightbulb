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
const usd = (x: number) => (Number.isNaN(x) ? "unknown" : `$${x.toFixed(2)}`);

/** Summarise a results dir into report.md (also returned). */
export function writeReport(resultsDir: string): string {
  const recs: RunRecord[] = readdirSync(resultsDir)
    .filter((f) => f.endsWith(".json") && f !== "controls.json")
    .map((f) => JSON.parse(readFileSync(join(resultsDir, f), "utf8")))
    .filter((r) => r.taskId);
  const cf = join(resultsDir, "controls.json");
  const controls: Controls | null = existsSync(cf) ? JSON.parse(readFileSync(cf, "utf8")) : null;

  const lines = ["# Apps eval report", "", `Builder: ${recs[0]?.builder ?? "?"} · model: ${recs[0]?.model ?? "?"} · ${recs.length} runs`, ""];
  if (!controls) lines.push("controls.json missing: the sanity controls were not recorded for this suite, so these scores are unverified.", "");
  else {
    if (!controls.referencePassed) lines.push("**RED: the reference app did not score 100% on its task.**", "");
    if (!controls.brokenFailedAllProbes) lines.push("**RED: the broken twin passed at least one security probe.**", "");
    if (controls.judgeOnReference < 0.9) lines.push(`**RED: the judge scored the reference app ${controls.judgeOnReference.toFixed(2)} (below 0.90); judge scores are suspect.**`, "");
    if (!controls.referencePassed || !controls.brokenFailedAllProbes) {
      lines.push("Scores withheld: a sanity control failed, so the suite cannot be trusted to measure builders.");
      return save(resultsDir, lines);
    }
  }

  const byTask = new Map<string, RunRecord[]>();
  for (const r of recs) byTask.set(r.taskId, [...(byTask.get(r.taskId) ?? []), r]);
  lines.push("| task | pass^3 | mean score | median minutes | median dollars | fix loops |", "|---|---|---|---|---|---|");
  let cubed = 0;
  for (const [id, rs] of [...byTask].sort(([a], [b]) => a.localeCompare(b))) {
    const p3 = rs.length >= 3 && rs.every((r) => r.passed); // pass^3: all three runs passed
    if (p3) cubed++;
    const mean = rs.reduce((a, r) => a + r.score, 0) / rs.length;
    lines.push(`| ${id} | ${p3 ? "yes" : "no"} | ${mean.toFixed(2)} | ${median(rs.map((r) => r.minutes)).toFixed(1)} | ${usd(median(rs.filter((r) => !r.costUnknown).map((r) => r.dollars)))} | ${rs.reduce((a, r) => a + r.fixLoops, 0)} |`);
  }
  const unknown = recs.filter((r) => r.costUnknown).length;
  const spend = recs.filter((r) => !r.costUnknown).reduce((a, r) => a + r.dollars, 0);
  const meanAll = recs.length ? recs.reduce((a, r) => a + r.score, 0) / recs.length : 0;
  lines.push("", "## Totals", "",
    `- pass^3: ${cubed} of ${byTask.size} tasks`,
    `- Mean score: ${meanAll.toFixed(2)} over ${recs.length} runs`,
    `- Median minutes per run: ${median(recs.map((r) => r.minutes)).toFixed(1)}`,
    `- Spend: ${usd(spend)}${unknown ? ` (plus ${unknown} run${unknown > 1 ? "s" : ""} with unknown cost, not included)` : ""}`,
    `- Fix loops: ${recs.reduce((a, r) => a + r.fixLoops, 0)}`);
  return save(resultsDir, lines);
}

function save(dir: string, lines: string[]): string {
  const md = lines.join("\n") + "\n";
  writeFileSync(join(dir, "report.md"), md);
  return md;
}
