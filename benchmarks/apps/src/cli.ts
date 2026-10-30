import { parseArgs } from "node:util";
import { claudeCodeAdapter } from "./adapters/claude-code";
import { codexAdapter } from "./adapters/codex";
import { judge } from "./judge";
import { runProbes } from "./probes";
import { writeReport } from "./report";
import { runSuite } from "./runner";

// pnpm tsx src/cli.ts suite [--adapter codex|claude-code] [--tasks a,b] [--runs 3]
const { positionals, values } = parseArgs({ allowPositionals: true, options: { adapter: { type: "string", default: "codex" }, tasks: { type: "string" }, runs: { type: "string" } } });
const adapter = { codex: codexAdapter, "claude-code": claudeCodeAdapter }[values.adapter!];
if (positionals[0] !== "suite" || !adapter) { console.error("usage: cli.ts suite [--adapter codex|claude-code] [--tasks a,b] [--runs 3]"); process.exit(2); }
const dir = await runSuite(adapter, { tasks: values.tasks?.split(","), runs: values.runs ? Number(values.runs) : undefined }, { probes: runProbes, judge });
writeReport(dir);
console.log(dir);
