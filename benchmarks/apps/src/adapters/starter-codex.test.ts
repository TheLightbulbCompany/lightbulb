import { describe, expect, it } from "vitest";
import { execFileSync } from "node:child_process";
import { EventEmitter } from "node:events";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { lightbulbStub, makeStarterCodexAdapter, prepareStarter, starterPrompt, type Exec } from "./starter-codex";

const sample = readFileSync(join(__dirname, "../__fixtures__/codex-sample.jsonl"), "utf8");

/** The parts of the starter the adapter edits, as they are in the CLI repo. */
function fakeStarter() {
  const s = mkdtempSync(join(tmpdir(), "starter-"));
  const put = (f: string, t: string) => { mkdirSync(join(s, f, ".."), { recursive: true }); writeFileSync(join(s, f), t); };
  put("AGENTS.md", "THE AGENTS RULES");
  put(".lightbulb/plan.md", "# THE PLAN");
  put("playbooks/form.md", "form");
  put("addons/content/x.ts", "never");
  put("node_modules/x/y.js", "never");
  put("app.yaml", "name: {{name}}\nopen_to: room\nkind: site\n");
  put("package.json", JSON.stringify({ name: "{{name}}", scripts: { check: "true" }, dependencies: {}, devDependencies: {} }));
  put("drizzle.config.ts", 'export default { out: "./drizzle", };');
  put("src/lib/db/index.ts", 'import { neon } from "@neondatabase/serverless";\nimport { drizzle as drizzleNeon } from "drizzle-orm/neon-http";\nconst d = drizzleNeon({ client: neon(process.env.DATABASE_URL!), schema });');
  return s;
}
const blankWork = () => {
  const w = mkdtempSync(join(tmpdir(), "work-"));
  mkdirSync(join(w, "app"));
  writeFileSync(join(w, "app/page.tsx"), "fixture");
  writeFileSync(join(w, "contract.json"), "{}");
  return w;
};
const task = { id: "b02-x", kind: "form", fixture: "blank", prompt: "THE REQUEST" } as any;

describe("prepareStarter", () => {
  it("replaces the blank fixture with the starter, keeps the contract, and leaves addons out", () => {
    const w = blankWork();
    prepareStarter(task, w, fakeStarter());
    expect(existsSync(join(w, "app"))).toBe(false);
    expect(existsSync(join(w, "contract.json"))).toBe(true);
    expect(existsSync(join(w, "addons"))).toBe(false);
    expect(existsSync(join(w, "node_modules"))).toBe(false);
    expect(readFileSync(join(w, "AGENTS.md"), "utf8")).toBe("THE AGENTS RULES");
    expect(JSON.parse(readFileSync(join(w, "package.json"), "utf8")).name).toBe("b02-x");
  });
  it("makes it servable by serve: open door, the task's kind, plain Postgres, a migrate URL", () => {
    const w = blankWork();
    prepareStarter(task, w, fakeStarter());
    expect(readFileSync(join(w, "app.yaml"), "utf8")).toBe("name: b02-x\nopen_to: public\nkind: form\n");
    expect(readFileSync(join(w, ".env.local"), "utf8")).toContain("LIGHTBULB_OPEN_TO=public");
    const db = readFileSync(join(w, "src/lib/db/index.ts"), "utf8");
    expect(db).toContain('from "pg"');
    expect(db).toContain('from "drizzle-orm/node-postgres"');
    expect(db).not.toMatch(/neon\(|serverless|neon-http/);
    expect(readFileSync(join(w, "drizzle.config.ts"), "utf8")).toContain("process.env.DATABASE_URL");
    expect(JSON.parse(readFileSync(join(w, "package.json"), "utf8")).dependencies.pg).toBeTruthy();
  });
  it("fails loudly when the starter moved out from under a shim", () => {
    const s = fakeStarter();
    writeFileSync(join(s, "drizzle.config.ts"), "export default {};");
    expect(() => prepareStarter(task, blankWork(), s)).toThrow(/starter shim/);
  });
  it("lays only the rules over a change task's fixture", () => {
    const w = blankWork();
    prepareStarter({ ...task, fixture: "portalis-m1" }, w, fakeStarter());
    expect(readFileSync(join(w, "app/page.tsx"), "utf8")).toBe("fixture");
    expect(existsSync(join(w, "AGENTS.md"))).toBe(true);
    expect(existsSync(join(w, ".lightbulb/plan.md"))).toBe(true);
    expect(existsSync(join(w, "src"))).toBe(false);
  });
});

it("the prompt carries the baseline, the starter's rules and plan, the playbook pointer, then the request", () => {
  const p = starterPrompt("BASELINE", "THE AGENTS RULES", "# THE PLAN", task);
  const at = ["BASELINE", "THE AGENTS RULES", "# THE PLAN", "playbooks/form.md", "THE REQUEST"].map((s) => p.indexOf(s));
  expect(at.every((n) => n >= 0)).toBe(true);
  expect(at).toEqual([...at].sort((a, b) => a - b));
});

it("the stub lightbulb refuses everything but app check", () => {
  const f = join(lightbulbStub(), "lightbulb");
  expect(() => execFileSync(f, ["app", "up"], { stdio: "pipe" })).toThrow();
  expect(() => execFileSync(f, ["login"], { stdio: "pipe" })).toThrow();
  expect(readFileSync(f, "utf8")).toContain('"app check"');
});

describe("the adapter", () => {
  const fake = (seen: any[]) => ((cmd: string, args: string[], o: any) => {
    const p: any = new EventEmitter();
    p.pid = 99999999; p.stdout = new EventEmitter(); p.stderr = new EventEmitter();
    seen.push({ cmd, args, o });
    setImmediate(() => { p.stdout.emit("data", sample); p.emit("close", 0); });
    return p;
  }) as any;
  const gate = (codes: number[], calls: string[]): Exec => async (cmd, args) => {
    calls.push(`${cmd} ${args.join(" ")}`);
    if (args[0] === "run") return { code: codes.shift() ?? 0, out: "src/a.ts(1,1): error TS1: boom" };
    return { code: 0, out: "abc" };
  };
  const setup = () => {
    process.env.LIGHTBULB_STARTER_DIR = fakeStarter();
    const w = blankWork();
    mkdirSync(join(w, "node_modules"));
    return w;
  };

  it("runs sandboxed with the stub first on PATH, gate passes first time, no second codex run", async () => {
    const seen: any[] = [], calls: string[] = [];
    const a = makeStarterCodexAdapter(fake(seen), gate([0], calls));
    const w = setup();
    const r = await a.build(task, w);
    expect(r.status).toBe("done");
    expect(seen).toHaveLength(1);
    const args = seen[0].args.join(" ");
    expect(args).toContain("--sandbox workspace-write");
    expect(args).not.toMatch(/dangerously|danger-full-access/);
    expect(args).toContain("THE AGENTS RULES");
    expect(seen[0].o.env.PATH.split(":")[0]).toMatch(/lightbulb-stub-/);
    expect(calls).toContain("pnpm run check");
    expect(r.log).toContain("[done gate] passed");
    expect(r.log).toContain("[starter:");
  });

  it("gives one fix loop on a red gate, with the failure in the prompt, and no more", async () => {
    const seen: any[] = [], calls: string[] = [];
    const a = makeStarterCodexAdapter(fake(seen), gate([1, 1], calls));
    const r = await a.build(task, setup());
    expect(seen).toHaveLength(2);
    expect(seen[1].args.at(-1)).toContain("error TS1: boom");
    expect(r.status).toBe("done");
    expect(r.log).toContain("[done gate after the fix loop] failed");
  });

  it("errors, without running codex, when the starter dir is missing", async () => {
    const seen: any[] = [];
    delete process.env.LIGHTBULB_STARTER_DIR;
    const r = await makeStarterCodexAdapter(fake(seen), gate([], [])).build(task, blankWork());
    expect(r.status).toBe("error");
    expect(seen).toHaveLength(0);
  });
});
