import { spawn as realSpawn } from "node:child_process";
import { chmodSync, cpSync, existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative } from "node:path";
import { countCodexFixLoops, parseCodexUsage } from "../cost";
import { writeBuildAllowlist } from "../serve";
import { loadRules } from "./claude-code";
import { codexArgs, runCodexRetrying } from "./codex";
import type { BuilderAdapter, BuildResult, Task } from "./types";

type Spawn = typeof realSpawn;
export type Exec = (cmd: string, args: string[], cwd: string, signal?: AbortSignal) => Promise<{ code: number; out: string }>;

const STEP_MS = 10 * 60_000;
/** A command on the host in its own process group, killed on abort or after ten minutes. Never throws. */
export const exec: Exec = (cmd, args, cwd, signal) => new Promise((done) => {
  let out = "";
  const p = realSpawn(cmd, args, { cwd, env: { ...process.env, NO_COLOR: "1" }, detached: true, stdio: ["ignore", "pipe", "pipe"] });
  const kill = () => { try { process.kill(-p.pid!, "SIGKILL"); } catch { /* gone */ } };
  const t = setTimeout(kill, STEP_MS);
  signal?.addEventListener("abort", kill, { once: true });
  p.stdout.on("data", (d) => (out += d));
  p.stderr.on("data", (d) => (out += d));
  p.on("error", (e) => { clearTimeout(t); done({ code: 1, out: out + String(e) }); });
  p.on("close", (c) => { clearTimeout(t); done({ code: c ?? 1, out }); });
});

const swap = (text: string, from: string, to: string) => {
  if (!text.includes(from)) throw new Error(`starter shim: "${from}" is gone from the starter; update starter-codex.ts`);
  return text.replace(from, to);
};

function* files(dir: string): Generator<string> {
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    if (e.name === ".git") continue;
    const p = join(dir, e.name);
    if (e.isDirectory()) yield* files(p); else yield p;
  }
}

/**
 * The starter as `lightbulb app new` lays it out, minus the content switch, plus what serve needs and the product gets elsewhere:
 * serve gives a plain Postgres on localhost, which the Neon HTTP driver cannot reach and `drizzle-kit migrate` needs a URL for,
 * and it has no Lightbulb pass, so the door stays open as for `open_to: public` (tasks bring their own sign-up).
 * A change task keeps its fixture app, which is not a starter app: only the rules are laid over it.
 */
export function prepareStarter(task: Pick<Task, "id" | "kind" | "fixture">, workdir: string, starter: string) {
  if (task.fixture !== "blank") {
    for (const f of ["AGENTS.md", ".lightbulb", "playbooks"]) cpSync(join(starter, f), join(workdir, f), { recursive: true });
    return;
  }
  for (const e of readdirSync(workdir)) if (![".git", "contract.json", "attachments"].includes(e)) rmSync(join(workdir, e), { recursive: true, force: true });
  cpSync(starter, workdir, { recursive: true, filter: (s) => !/(^|\/)(node_modules|\.next|addons)(\/|$)/.test(relative(starter, s)) });
  for (const f of files(workdir)) {
    const text = readFileSync(f, "utf8");
    if (text.includes("{{name}}")) writeFileSync(f, text.replaceAll("{{name}}", task.id));
  }
  const edit = (f: string, fn: (s: string) => string) => writeFileSync(join(workdir, f), fn(readFileSync(join(workdir, f), "utf8")));
  edit("app.yaml", (s) => swap(swap(s, "kind: site", `kind: ${task.kind}`), "open_to: room", "open_to: public"));
  edit("src/lib/db/index.ts", (s) => swap(swap(swap(s,
    'import { neon } from "@neondatabase/serverless";', 'import { Pool } from "pg";'),
    'from "drizzle-orm/neon-http";', 'from "drizzle-orm/node-postgres";'),
    "client: neon(process.env.DATABASE_URL!)", "client: new Pool({ connectionString: process.env.DATABASE_URL! })"));
  edit("drizzle.config.ts", (s) => swap(s, 'out: "./drizzle",', 'out: "./drizzle",\n  dbCredentials: { url: process.env.DATABASE_URL ?? "" },'));
  edit("package.json", (s) => {
    const pkg = JSON.parse(s);
    pkg.dependencies.pg = "8.23.1";
    pkg.devDependencies["@types/pg"] = "8.23.1";
    return JSON.stringify(pkg, null, 2) + "\n";
  });
  writeFileSync(join(workdir, ".env.local"), `LIGHTBULB_OPEN_TO=public\nLIGHTBULB_APP=${task.id}\nLIGHTBULB_COOKIE_SECRET=eval-cookie-secret-eval-cookie-secret\n`);
}

/** A `lightbulb` first on the builder's PATH: `app check` works, nothing can reach production. Returns the directory. */
export function lightbulbStub() {
  const dir = mkdtempSync(join(tmpdir(), "lightbulb-stub-"));
  const f = join(dir, "lightbulb");
  writeFileSync(f, '#!/bin/sh\nif [ "$1 $2" = "app check" ]; then exec pnpm run check; fi\necho "lightbulb: only `lightbulb app check` works in this run: there is no room and no deploy." >&2\nexit 2\n');
  chmodSync(f, 0o755);
  return dir;
}

const LOCAL_RUN = (changeTask: boolean) => `## This run

It runs on one computer. There is no room, no brand, no preview and no one to ask; \`lightbulb app up\`, \`status\` and \`evidence\` do not exist here, so make your own choices and treat every step that needs them as skipped. Your last step is \`pnpm check\` passing (\`lightbulb app check\` runs the same), then \`pnpm build\`; a check repeats it after you finish.
${changeTask
    ? "The app in this folder already exists and was not made from the Lightbulb starter. Keep its stack and layout; the rules above about `src/`, the door and the UI kit apply only where this app has them. It may have no `check` script: then the check is `pnpm exec tsc --noEmit`."
    : "The app is served from this folder on `DATABASE_URL`: `getDb()` reaches it, and migrations from `pnpm db:generate` are applied by `pnpm drizzle-kit migrate`. It starts with no rows and without the seed. The door is already open for this app (`open_to: public`, `.env.local`); that is not you loosening it."}`;

/** The baseline's prompt with the starter's rules and the task's own kind of playbook pointed at, ahead of the request. */
export const starterPrompt = (baselineRules: string, agents: string, plan: string, task: Pick<Task, "prompt" | "kind" | "fixture">) =>
  `${baselineRules}\n\n# The app's own rules (AGENTS.md)\n\n${agents}\n\n# The plan file (.lightbulb/plan.md)\n\n${plan}\n\nThis app's kind is \`${task.kind}\`: read \`playbooks/${task.kind}.md\`.\n\n${LOCAL_RUN(task.fixture !== "blank")}\n\n# The request\n\n${task.prompt}`;

const fixPrompt = (prompt: string, failure: string) =>
  `${prompt}\n\n# Your first pass is in this folder and its check failed\n\nFix what it reports and run it again until it passes. Do not weaken the check, the lint rules or the TypeScript settings.\n\n\`\`\`\n${failure.slice(-6000)}\n\`\`\``;

export async function doneGate(workdir: string, signal: AbortSignal | undefined, run: Exec) {
  if (!existsSync(join(workdir, "node_modules"))) {
    writeBuildAllowlist(workdir); // builder output is untrusted and this install runs on the host, as in serve
    const i = await run("pnpm", ["install"], workdir, signal);
    if (i.code !== 0) return i;
  }
  const scripts = JSON.parse(readFileSync(join(workdir, "package.json"), "utf8")).scripts ?? {};
  return run("pnpm", scripts.check ? ["run", "check"] : ["exec", "tsc", "--noEmit"], workdir, signal);
}

export const makeStarterCodexAdapter = (spawn: Spawn = realSpawn, run: Exec = exec): BuilderAdapter => ({
  name: "starter-codex", billing: "subscription", defaultModel: "codex-default",
  async build(task, workdir, signal) {
    const model = process.env.EVAL_MODEL;
    const start = Date.now();
    let out = "", header = "", stub = "";
    const result = (status: BuildResult["status"], extra = ""): BuildResult => {
      const u = parseCodexUsage(out);
      return { dir: workdir, tokensIn: u?.tokensIn ?? 0, tokensOut: u?.tokensOut ?? 0, dollars: 0, billing: "subscription", minutes: (Date.now() - start) / 60_000, fixLoops: countCodexFixLoops(out), status, log: `${header}${extra}${out}` };
    };
    try {
      const starter = process.env.LIGHTBULB_STARTER_DIR;
      if (!starter || !existsSync(join(starter, "AGENTS.md"))) throw new Error("LIGHTBULB_STARTER_DIR must point at the CLI repo's starter/");
      const sha = (await run("git", ["rev-parse", "--short", "HEAD"], starter)).out.trim();
      const rules = await loadRules();
      header = `[rules source: ${rules.source}] [starter: ${starter} @ ${sha}] [model: ${model ?? "codex-default"}]\n`;
      prepareStarter(task, workdir, starter);
      if (task.fixture === "blank") {
        const lock = await run("pnpm", ["install", "--lockfile-only"], workdir, signal);
        if (lock.code !== 0) return result("error", `[error] lockfile: ${lock.out}\n`);
      }
      stub = lightbulbStub();
      const env = { PATH: `${stub}:${process.env.PATH}` };
      const prompt = starterPrompt(rules.text, readFileSync(join(starter, "AGENTS.md"), "utf8"), readFileSync(join(starter, ".lightbulb/plan.md"), "utf8"), task);
      const codex = async (p: string) => {
        const r = await runCodexRetrying(codexArgs(p, workdir, model), workdir, signal, spawn, () => { header += "[retry] model at capacity\n"; }, env);
        out += r.out;
        return r;
      };
      const first = await codex(prompt);
      if (signal?.aborted) return result("timeout");
      if (first.code !== 0 || !parseCodexUsage(out)) return result("error");
      let gate = await doneGate(workdir, signal, run);
      header += `[done gate] ${gate.code === 0 ? "passed" : "failed"}\n`;
      if (gate.code !== 0 && !signal?.aborted) {
        const fix = await codex(fixPrompt(prompt, gate.out));
        if (signal?.aborted) return result("timeout");
        if (fix.code !== 0) return result("error");
        gate = await doneGate(workdir, signal, run);
        header += `[done gate after the fix loop] ${gate.code === 0 ? "passed" : "failed"}\n${gate.code === 0 ? "" : gate.out.slice(-2000) + "\n"}`;
      }
      return result(signal?.aborted ? "timeout" : "done"); // a gate still red is scored as it stands: serve and the journeys decide
    } catch (e) {
      return result("error", `[error] ${String(e)}\n`);
    } finally {
      if (stub) rmSync(stub, { recursive: true, force: true });
    }
  },
});
export const starterCodexAdapter = makeStarterCodexAdapter();
