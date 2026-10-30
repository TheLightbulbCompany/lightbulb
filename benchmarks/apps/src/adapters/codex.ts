import { spawn as realSpawn } from "node:child_process";
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
/** Run codex with stdin ignored in its own process group; abort kills the group. Never throws; resolves with output and exit code. */
export function runCodex(args: string[], cwd: string, signal?: AbortSignal, spawn: Spawn = realSpawn): Promise<{ code: number; out: string }> {
  return new Promise((done) => {
    let out = "";
    try {
      const p = spawn("codex", args, { cwd, detached: true, stdio: ["ignore", "pipe", "pipe"] });
      const kill = () => { try { process.kill(-p.pid!, "SIGKILL"); } catch { /* gone */ } };
      signal?.addEventListener("abort", kill, { once: true });
      p.stdout?.on("data", (d) => (out += d));
      p.stderr?.on("data", (d) => (out += d));
      p.on("error", (e) => { out += String(e); done({ code: 1, out }); });
      p.on("close", (c) => done({ code: c ?? 1, out }));
    } catch (e) { done({ code: 1, out: String(e) }); }
  });
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
      const r = await runCodex(codexArgs(`${rules.text}\n\n${task.prompt}`, workdir, model), workdir, signal, spawn);
      out = r.out;
      return result(signal?.aborted ? "timeout" : r.code === 0 && parseCodexUsage(out) ? "done" : "error");
    } catch (e) {
      return result("error", `[error] ${String(e)}\n`);
    }
  },
});
export const codexAdapter = makeCodexAdapter();
