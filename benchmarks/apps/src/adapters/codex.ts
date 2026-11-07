import { spawn as realSpawn } from "node:child_process";
import { chmodSync, copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import { countCodexFixLoops, parseCodexUsage } from "../cost";
import { loadRules } from "./claude-code";
import type { BuilderAdapter, BuildResult } from "./types";

/** Sandboxed on purpose: writes only inside the workdir, network on for `pnpm install`. Never the bypass flags. */
export const codexArgs = (prompt: string, workdir: string, model?: string, sandbox = "workspace-write") => [
  "exec", "--json", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
  "--sandbox", sandbox, ...(sandbox === "workspace-write" ? ["-c", "sandbox_workspace_write.network_access=true"] : []),
  "-C", workdir, ...(model ? ["-m", model] : []), prompt,
];

type Spawn = typeof realSpawn;

/**
 * A throwaway HOME (and CODEX_HOME inside it) holding only a 0600 copy of the operator's auth.json.
 * --ignore-user-config still loads ~/.codex/{skills,AGENTS.md,hooks.json} and ~/.agents/skills; the baseline is
 * "workspace rules + the model", so none of the operator's own files may reach the run.
 */
export function isolatedHome(realCodexHome = process.env.CODEX_HOME ?? join(homedir(), ".codex")) {
  const home = mkdtempSync(join(tmpdir(), "codex-home-"));
  const codexHome = join(home, ".codex");
  mkdirSync(codexHome);
  const real = join(realCodexHome, "auth.json"), copy = join(codexHome, "auth.json");
  if (existsSync(real)) { copyFileSync(real, copy); chmodSync(copy, 0o600); }
  const before = existsSync(copy) ? readFileSync(copy, "utf8") : "";
  const env = { ...process.env, HOME: home, CODEX_HOME: codexHome, PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: "1" };
  const cleanup = () => {
    try { // codex may rotate the refresh token mid-run: keep the new login, or the operator's copy goes stale
      const after = existsSync(copy) ? readFileSync(copy, "utf8") : before;
      if (after !== before) { JSON.parse(after); copyFileSync(copy, real); chmodSync(real, 0o600); }
    } catch { /* unreadable or half-written: keep the operator's file */ }
    rmSync(home, { recursive: true, force: true });
  };
  return { env, cleanup };
}

/** Run codex isolated (see isolatedHome) with stdin ignored in its own process group; abort kills the group. Never throws; resolves with output and exit code. */
export function runCodex(args: string[], cwd: string, signal?: AbortSignal, spawn: Spawn = realSpawn, extraEnv: NodeJS.ProcessEnv = {}): Promise<{ code: number; out: string }> {
  return new Promise((resolve) => {
    let out = "";
    let iso: ReturnType<typeof isolatedHome> | undefined;
    const done = (r: { code: number; out: string }) => { iso?.cleanup(); iso = undefined; resolve(r); };
    try {
      iso = isolatedHome();
      const p = spawn("codex", args, { cwd, env: { ...iso.env, ...extraEnv }, detached: true, stdio: ["ignore", "pipe", "pipe"] });
      const kill = () => { try { process.kill(-p.pid!, "SIGKILL"); } catch { /* gone */ } };
      signal?.addEventListener("abort", kill, { once: true });
      if (signal?.aborted) kill();
      p.stdout?.on("data", (d) => (out += d));
      p.stderr?.on("data", (d) => (out += d));
      p.on("error", (e) => { out += String(e); done({ code: 1, out }); });
      p.on("close", (c) => done({ code: c ?? 1, out }));
    } catch (e) { done({ code: 1, out: String(e) }); }
  });
}

const AT_CAPACITY = /Selected model is at capacity/;

/** runCodex with one retry after a pause when the provider is at capacity (not the builder's fault). */
export async function runCodexRetrying(args: string[], cwd: string, signal: AbortSignal | undefined, spawn: Spawn, onRetry: () => void, extraEnv?: NodeJS.ProcessEnv) {
  const r = await runCodex(args, cwd, signal, spawn, extraEnv);
  if (signal?.aborted || !AT_CAPACITY.test(r.out)) return r;
  onRetry();
  await new Promise((f) => setTimeout(f, Number(process.env.LIGHTBULB_EVAL_RETRY_MS ?? 60_000)));
  return runCodex(args, cwd, signal, spawn, extraEnv);
}

export const makeCodexAdapter = (spawn: Spawn = realSpawn): BuilderAdapter => ({
  name: "codex", billing: "subscription", defaultModel: "codex-default",
  async build(task, workdir, signal) {
    const model = process.env.EVAL_MODEL;
    const start = Date.now();
    let out = "", header = "";
    const result = (status: BuildResult["status"], extra = ""): BuildResult => {
      const u = parseCodexUsage(out);
      return { dir: workdir, tokensIn: u?.tokensIn ?? 0, tokensOut: u?.tokensOut ?? 0, dollars: 0, billing: "subscription", minutes: (Date.now() - start) / 60_000, fixLoops: countCodexFixLoops(out), status, log: `${header}${extra}${out}` };
    };
    try {
      const rules = await loadRules();
      header = `[rules source: ${rules.source}] [model: ${model ?? "codex-default"}]\n`;
      const args = codexArgs(`${rules.text}\n\n${task.prompt}`, workdir, model);
      const r = await runCodexRetrying(args, workdir, signal, spawn, () => { header += "[retry] model at capacity\n"; });
      out = r.out;
      return result(signal?.aborted ? "timeout" : r.code === 0 && parseCodexUsage(out) ? "done" : "error");
    } catch (e) {
      return result("error", `[error] ${String(e)}\n`);
    }
  },
});
export const codexAdapter = makeCodexAdapter();
