import { execFile, spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { countFixLoops, parseResult } from "../cost";
import type { BuilderAdapter, BuildResult } from "./types";

const here = dirname(fileURLToPath(import.meta.url));
const RULES_PATH = "apps/infra/hermes/skills-src/lightbulb/references/software-projects.md";
export const RUN_CAP_USD = 15;
export const STRIPPED = ["Repository", "Deploy to Vercel", "AWS within the connector's limits", "Continuous integration", "Pull-request review", "A database, hosting or another paid service"];
export const STAGE0_NOTE = "## How this app will be run\nPostgres is available at runtime in `DATABASE_URL`. If you need tables, use Drizzle with a `drizzle.config.ts` and generated migrations; they are applied with `pnpm drizzle-kit migrate` before the build. The app is started with `pnpm build && pnpm start` and served over plain http at `http://localhost:<port>`. `CRON_SECRET` is set in the environment. Install with pnpm, not npm or yarn; this machine's pnpm refuses packages published in the last 24 hours, so if it refuses a version, pick an older one. Do not download browsers (no `playwright install`); the checks bring their own. The names the checks rely on are in `contract.json`; do not change that file.";

export function buildRules(md: string): string {
  const kept = md.split(/^(?=## )/m).filter((s) => !STRIPPED.some((h) => s.startsWith(`## ${h}\n`)));
  return kept.join("").trimEnd() + "\n\n" + STAGE0_NOTE;
}

let cached: Promise<{ text: string; source: string }> | undefined;
/** Today's rules from origin/main (working tree if git fails); memoized so every run in a suite sees the same text. */
export function loadRules(): Promise<{ text: string; source: string }> {
  return (cached ??= (async () => {
    try {
      const { stdout } = await promisify(execFile)("git", ["show", `origin/main:${RULES_PATH}`], { cwd: here, maxBuffer: 10_000_000 });
      return { text: buildRules(stdout), source: "origin/main" };
    } catch {
      return { text: buildRules(readFileSync(join(here, "../../../..", RULES_PATH), "utf8")), source: "working-tree" };
    }
  })());
}

export const claudeArgs = (prompt: string, model: string) =>
  ["--bare", "-p", prompt, "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions", "--model", model, "--max-budget-usd", String(RUN_CAP_USD)];

export const claudeCodeAdapter: BuilderAdapter = {
  name: "claude-code", billing: "api", defaultModel: "claude-sonnet-5",
  async build(task, workdir, signal) {
    const model = process.env.EVAL_MODEL ?? "claude-sonnet-5";
    const start = Date.now();
    let out = "";
    let header = "";
    const result = (status: BuildResult["status"], extra = ""): BuildResult => {
      const r = parseResult(out);
      return {
        dir: workdir, tokensIn: r?.tokensIn ?? 0, tokensOut: r?.tokensOut ?? 0, dollars: r?.dollars ?? 0, billing: "api", costUnknown: !r && !!out,
        minutes: (Date.now() - start) / 60_000, fixLoops: countFixLoops(out), status,
        log: `${header}${extra}${out}`,
      };
    };
    if (!process.env.ANTHROPIC_API_KEY) return result("error", "[error] --bare only authenticates with ANTHROPIC_API_KEY, which is not set; nothing was spawned\n");
    try {
      const rules = await loadRules();
      header = `[rules source: ${rules.source}] [model: ${model}]\n`;
      const code = await new Promise<number>((done) => {
        const p = spawn("claude", claudeArgs(`${rules.text}\n\n${task.prompt}`, model), { cwd: workdir, detached: true, stdio: ["ignore", "pipe", "pipe"] });
        const kill = () => { try { process.kill(-p.pid!, "SIGKILL"); } catch { /* gone */ } };
        signal?.addEventListener("abort", kill, { once: true });
        p.stdout.on("data", (d) => (out += d));
        p.stderr.on("data", (d) => (out += d));
        p.on("error", (e) => { out += String(e); done(1); });
        p.on("close", (c) => done(c ?? 1));
      });
      return result(signal?.aborted ? "timeout" : code === 0 && parseResult(out) ? "done" : "error");
    } catch (e) {
      return result("error", `[error] ${String(e)}\n`); // never throws: the runner must still see partial cost
    }
  },
};
