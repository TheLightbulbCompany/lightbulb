import { cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { ProbeResult, ProbeSpec } from "./adapters/types";
import { judge as realJudge } from "./judge";
import { runProbes } from "./probes";
import { loadTask, playwrightJourneys, type Deps } from "./runner";
import { serve as realServe } from "./serve";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
/** The planted key in the broken twin's client component. */
export const PLANTED_SECRET = "sk_test_EVALBROKEN000000000000";
const TASK = "b01-todo";

/**
 * Sanity controls (Dyad): the hand-written reference must pass every b01 journey and probe; the broken twin must fail every probe.
 * The destructive-migration kind needs a change task's seeded fixture, so it is not run here.
 */
export async function runControls(deps: Partial<Deps> = {}): Promise<{ referencePassed: boolean; brokenFailedAllProbes: boolean; judgeOnReference: number; detail: string }> {
  const serve = deps.serve ?? realServe;
  const probes = deps.probes ?? runProbes;
  const journeys = deps.journeys ?? playwrightJourneys;
  const judge = deps.judge ?? realJudge;
  const base = loadTask(TASK);
  const task = { ...base, secrets: [PLANTED_SECRET] };
  const secretProbe: ProbeSpec = { id: "secret-client", kind: "secret-in-client", target: "/" };
  // sql-injection last: its payloads may take the twin's app down
  const set = [...task.probes.filter((p) => p.kind !== "sql-injection"), secretProbe, ...task.probes.filter((p) => p.kind === "sql-injection")];

  /** Serve a copy of a control app (never the checked-in dir) and always stop and delete it. */
  async function withApp<T>(name: string, fn: (url: string, dir: string, databaseUrl: string) => Promise<T>): Promise<T | { error: string }> {
    const dir = mkdtempSync(join(tmpdir(), `controls-${name}-`));
    let s: Awaited<ReturnType<typeof serve>> | undefined;
    try {
      cpSync(join(root, "controls", name), dir, { recursive: true, filter: (f) => !/(^|\/)(node_modules|\.next)(\/|$)/.test(f) });
      writeFileSync(join(dir, "contract.json"), readFileSync(join(root, "tasks", TASK, "contract.json")));
      s = await serve(dir);
      if (!s.built) return { error: `did not build or start: ${s.buildLog.slice(-1500)}` };
      return await fn(s.url, dir, s.databaseUrl);
    } finally {
      await s?.stop();
      rmSync(dir, { recursive: true, force: true });
    }
  }
  const lines = (rs: ProbeResult[]) => rs.map((r) => `  ${r.id}: ${r.pass ? "PASS" : "FAIL"} ${r.detail}`);

  const detail: string[] = [];
  let judgeOnReference = 0;
  const ref = await withApp(`${TASK}-reference`, async (url, dir, databaseUrl) => {
    const j = await journeys(task, url);
    judgeOnReference = await judge(url, dir, task); // before probes: sql-injection writes to the db
    const p = await probes(url, dir, set, databaseUrl, task);
    return { j, p };
  });
  let referencePassed = false;
  if ("error" in ref) detail.push(`reference: ${ref.error}`);
  else {
    referencePassed = ref.j.length > 0 && ref.j.every((x) => x.pass) && ref.p.every((x) => x.pass);
    detail.push(`reference: journeys ${ref.j.map((x) => `${x.id}: ${x.pass ? "PASS" : "FAIL"}`).join("; ") || "none ran"}`, ...lines(ref.p));
  }

  const broken = await withApp(`${TASK}-broken`, (url, dir, databaseUrl) => probes(url, dir, set, databaseUrl, task));
  let brokenFailedAllProbes = false;
  if ("error" in broken) detail.push(`broken: ${broken.error}`);
  else {
    brokenFailedAllProbes = broken.length === set.length && broken.every((x) => !x.pass);
    detail.push("broken:", ...lines(broken));
  }
  return { referencePassed, brokenFailedAllProbes, judgeOnReference, detail: detail.join("\n") };
}
