import { describe, expect, it } from "vitest";
import { EventEmitter } from "node:events";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { countCodexFixLoops, parseCodexUsage } from "../cost";
import { codexArgs, isolatedHome, makeCodexAdapter } from "./codex";

const sample = readFileSync(join(__dirname, "../__fixtures__/codex-sample.jsonl"), "utf8");
const ev = (o: unknown) => JSON.stringify(o);
const fail = { type: "item.completed", item: { type: "command_execution", status: "failed", exit_code: 1 } };
const edit = (path: string) => ({ type: "item.completed", item: { type: "file_change", status: "completed", changes: [{ path, kind: "update" }] } });
const turn = (i: number, o: number, r = 0) => ({ type: "turn.completed", usage: { input_tokens: i, output_tokens: o, reasoning_output_tokens: r } });

describe("codex stream", () => {
  it("sums tokens over turns, counting reasoning as output", () => {
    expect(parseCodexUsage([turn(100, 5, 10), turn(200, 7)].map(ev).join("\n"))).toEqual({ tokensIn: 300, tokensOut: 22 });
    expect(parseCodexUsage("garbage")).toBeNull();
    expect(parseCodexUsage(sample)).toEqual({ tokensIn: 47136, tokensOut: 87 });
  });
  it("counts a fix loop when a failed command later passes, or a file is re-edited after a failure", () => {
    const run = (command: string, exit_code: number) => ({ type: "item.completed", item: { type: "command_execution", command, status: exit_code ? "failed" : "completed", exit_code } });
    expect(countCodexFixLoops([run("pnpm build", 1), run("sed -i x a.ts", 0), run("pnpm build", 0)].map(ev).join("\n"))).toBe(1);
    expect(countCodexFixLoops([run("pnpm build", 1), run("pnpm lint", 0)].map(ev).join("\n"))).toBe(0);
    expect(countCodexFixLoops([edit("a"), fail, edit("a"), edit("a")].map(ev).join("\n"))).toBe(1);
    expect(countCodexFixLoops([edit("a"), fail, edit("b")].map(ev).join("\n"))).toBe(0);
  });
});

describe("codex adapter", () => {
  it("runs sandboxed in the workdir and never bypasses the sandbox", () => {
    const a = codexArgs("P", "/w", "m");
    expect(a).toEqual(expect.arrayContaining(["--json", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check", "--sandbox", "workspace-write", "sandbox_workspace_write.network_access=true", "-C", "/w", "-m", "m"]));
    expect(a[a.indexOf("-C") + 1]).toBe("/w");
    expect(a.join(" ")).not.toMatch(/dangerously|danger-full-access/);
    expect(a.at(-1)).toBe("P");
  });

  const fake = (script: (p: any) => void) => ((cmd: string, args: string[], o: any) => {
    const p: any = new EventEmitter();
    p.pid = 99999999; p.stdout = new EventEmitter(); p.stderr = new EventEmitter(); p.spawned = { cmd, args, o };
    setImmediate(() => script(p));
    return p;
  }) as any;
  const task = { id: "x", prompt: "do it" } as any;
  const dir = mkdtempSync(join(tmpdir(), "cx-"));

  it("retries once when the model is at capacity", async () => {
    process.env.LIGHTBULB_EVAL_RETRY_MS = "1";
    let n = 0;
    const a = makeCodexAdapter(fake((p) => { n++; p.stdout.emit("data", n === 1 ? '{"type":"turn.failed","error":{"message":"Selected model is at capacity. Please try a different model."}}\n' : sample); p.emit("close", n === 1 ? 1 : 0); }));
    const r = await a.build(task, dir);
    expect(n).toBe(2);
    expect(r.status).toBe("done");
    expect(r.log).toContain("[retry] model at capacity");
  });

  it("parses a run: subscription, zero dollars, not unknown cost", async () => {
    const a = makeCodexAdapter(fake((p) => { p.stdout.emit("data", sample); p.emit("close", 0); }));
    const r = await a.build(task, dir);
    expect(r).toMatchObject({ status: "done", billing: "subscription", dollars: 0, tokensIn: 47136, tokensOut: 87 });
    expect(r.costUnknown).toBeUndefined();
  });
  it("spawns with stdin ignored, detached", async () => {
    let seen: any;
    await makeCodexAdapter(fake((p) => { seen = p.spawned; p.emit("close", 1); })).build(task, dir);
    expect(seen.cmd).toBe("codex");
    expect(seen.o).toMatchObject({ detached: true, stdio: ["ignore", "pipe", "pipe"] });
  });
  it("spawns in a throwaway HOME/CODEX_HOME holding only the login, deleted afterwards", async () => {
    let seen: any, home = "";
    await makeCodexAdapter(fake((p) => { seen = p.spawned; home = seen.o.env.HOME; p.emit("close", 0); })).build(task, dir);
    expect(home).not.toBe(process.env.HOME);
    expect(seen.o.env.CODEX_HOME).toBe(join(home, ".codex"));
    expect(seen.o.env.PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD).toBe("1");
    expect(existsSync(home)).toBe(false);
  });
  it("isolatedHome copies auth.json 0600 and writes a rotated login back", () => {
    const real = mkdtempSync(join(tmpdir(), "cxh-"));
    writeFileSync(join(real, "auth.json"), '{"t":1}');
    mkdirSync(join(real, "skills"));
    const iso = isolatedHome(real);
    const copy = join(iso.env.CODEX_HOME!, "auth.json");
    expect(statSync(copy).mode & 0o777).toBe(0o600);
    expect(existsSync(join(iso.env.CODEX_HOME!, "skills"))).toBe(false);
    writeFileSync(copy, '{"t":2}');
    iso.cleanup();
    expect(readFileSync(join(real, "auth.json"), "utf8")).toBe('{"t":2}');
    expect(existsSync(iso.env.HOME!)).toBe(false);
  });
  it("a spawn error does not throw", async () => {
    const r = await makeCodexAdapter(fake((p) => p.emit("error", new Error("ENOENT")))).build(task, dir);
    expect(r.status).toBe("error");
    expect(r.log).toContain("ENOENT");
    const r2 = await makeCodexAdapter((() => { throw new Error("boom"); }) as any).build(task, dir);
    expect(r2.status).toBe("error");
  });
  it("abort kills the process group and reports timeout", async () => {
    const kills: any[] = [];
    const orig = process.kill;
    process.kill = ((pid: number, sig: string) => { kills.push([pid, sig]); }) as any;
    try {
      const ac = new AbortController();
      const a = makeCodexAdapter(fake((p) => { ac.abort(); p.emit("close", null); }));
      const r = await a.build(task, dir, ac.signal);
      expect(kills).toEqual([[-99999999, "SIGKILL"]]);
      expect(r.status).toBe("timeout");
    } finally { process.kill = orig; }
  });
});
