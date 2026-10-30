import { parseArgs } from "node:util";
import { claudeCodeAdapter } from "./adapters/claude-code";
import { codexAdapter } from "./adapters/codex";
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { runControls } from "./controls";
import { judge } from "./judge";
import { runProbes } from "./probes";
import { writeReport } from "./report";
import { runSuite } from "./runner";

// pnpm tsx src/cli.ts suite [--adapter codex|claude-code] [--tasks a,b] [--runs 3]
const { positionals, values } = parseArgs({ allowPositionals: true, options: { adapter: { type: "string", default: "codex" }, tasks: { type: "string" }, runs: { type: "string" } } });
const adapter = { codex: codexAdapter, "claude-code": claudeCodeAdapter }[values.adapter!];
if (positionals[0] !== "suite" || !adapter) { console.error("usage: cli.ts suite [--adapter codex|claude-code] [--tasks a,b] [--runs 3]"); process.exit(2); }
const dir = await runSuite(adapter, { tasks: values.tasks?.split(","), runs: values.runs ? Number(values.runs) : undefined,
  // the report withholds scores when controls.json is missing or red
  onStart: async (out: string) => {
    const { detail, ...c } = await runControls();
    writeFileSync(join(out, "controls.json"), JSON.stringify(c, null, 2));
    writeFileSync(join(out, "controls.log"), detail);
    console.error(`controls: reference ${c.referencePassed ? "ok" : "FAILED"}, broken twin ${c.brokenFailedAllProbes ? "ok" : "FAILED"}, judge ${c.judgeOnReference}`);
  } }, { probes: runProbes, judge });
writeReport(dir);
console.log(dir);
