import { describe, expect, it } from "vitest";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { parse } from "yaml";
import type { Task } from "./adapters/types";

const root = join(__dirname, "..");
const tasksDir = join(root, "tasks");
const ids = existsSync(tasksDir) ? readdirSync(tasksDir).filter((d) => !d.startsWith("_")).sort() : [];
const load = (id: string) => parse(readFileSync(join(tasksDir, id, "task.yaml"), "utf8")) as Task;
const tasks = ids.map(load);

describe("tasks", () => {
  it("every task.yaml parses and has journeys", () => {
    expect(ids.length).toBe(20);
    for (const t of tasks) {
      expect(t.id, t.id).toBe(ids[tasks.indexOf(t)]);
      expect(t.prompt.length).toBeGreaterThan(200);
      expect(t.journeys).toBe("journeys.spec.ts");
      expect(existsSync(join(tasksDir, t.id, t.journeys)), t.id).toBe(true);
      expect(t.timeoutMin).toBe(45);
      expect(Array.isArray(t.probes)).toBe(true);
    }
  });

  it("there are exactly 10 build, 7 change, 3 adversarial", () => {
    const n = (k: string) => tasks.filter((t) => t.type === k).length;
    expect([n("build"), n("change"), n("adversarial")]).toEqual([10, 7, 3]);
  });

  it("every change task names a -m1 fixture, and build tasks use blank", () => {
    for (const t of tasks) {
      if (t.type === "change") expect(t.fixture, t.id).toMatch(/^(todo|slotline|deskhero|ledgerly|portalis|relay-crm|curbside)-m1$/);
      else if (t.id !== "a03-destroy") expect(t.fixture, t.id).toBe("blank");
    }
    expect(existsSync(join(root, "fixtures/blank/package.json"))).toBe(true);
    expect(existsSync(join(root, "fixtures/blank/app/page.tsx"))).toBe(true);
  });

  it("change tasks start from their own app's fixture, and probes are present", () => {
    const want: Record<string, string> = { "c01-todo": "todo-m1", "c02-slotline": "slotline-m1", "c03-deskhero": "deskhero-m1", "c04-ledgerly": "ledgerly-m1", "c05-portalis": "portalis-m1", "c06-relaycrm": "relay-crm-m1", "c07-curbside": "curbside-m1", "a03-destroy": "relay-crm-m1" };
    for (const [id, f] of Object.entries(want)) expect(tasks.find((t) => t.id === id)!.fixture, id).toBe(f);
    for (const t of tasks) if (t.type !== "build") expect(existsSync(join(root, "fixtures", t.fixture, "package.json")), `${t.id}: fixtures/${t.fixture}`).toBe(true);
    for (const t of tasks) {
      expect(t.probes.length, t.id).toBeGreaterThan(0);
      for (const p of t.probes) expect(p.id, t.id).toMatch(/^[a-z]+(-[a-z0-9]+)+$|^secret-in-client$/);
    }
  });

  it("no task id mentions mobile", () => {
    for (const t of tasks) expect(`${t.id} ${t.prompt}`.toLowerCase()).not.toContain("mobile");
  });

  it("contract.json is stated verbatim in the prompt and covers every testid the journeys use", () => {
    for (const t of tasks) {
      const c = JSON.parse(readFileSync(join(tasksDir, t.id, "contract.json"), "utf8")) as { routes: string[]; testids: string[] };
      for (const x of [...c.routes, ...c.testids]) expect(t.prompt, `${t.id} ${x}`).toContain("`" + x + "`");
      const spec = readFileSync(join(tasksDir, t.id, t.journeys), "utf8");
      const usesAuth = /\b(signUp|newUser|signIn|signOut)\(/.test(spec); // the shared helpers drive the auth test ids
      const j = spec + (usesAuth ? readFileSync(join(tasksDir, "_shared/helpers.ts"), "utf8") : "");
      const used = [...j.matchAll(/getByTestId\(\s*["'`]([^"'`$]+)["'`]/g)].map((m) => m[1]);
      expect(used.length, t.id).toBeGreaterThan(0);
      used.push(...[...spec.matchAll(/data-testid=\\?"([^"\\]+)\\?"/g)].map((m) => m[1]));
      for (const u of used) expect(c.testids, `${t.id} uses ${u}`).toContain(u);
    }
  });

  it("a01-secret lists its planted fake key", () => {
    const t = tasks.find((x) => x.id === "a01-secret")!;
    expect(t.secrets?.[0]).toMatch(/^sk_test_/);
    expect(t.prompt).toContain(t.secrets![0]);
  });
  it("a journey that calls me() (GET /api/me) only exists where the prompt asks for /api/me", () => {
    for (const t of tasks) {
      const spec = readFileSync(join(tasksDir, t.id, t.journeys), "utf8");
      if (/\bme\(/.test(spec)) expect(t.prompt, t.id).toContain("/api/me");
    }
  });
});
