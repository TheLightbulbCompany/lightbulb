import { execFile } from "node:child_process";
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { parse } from "yaml";
import { RUN_CAP_USD } from "./adapters/claude-code";
import type { BuilderAdapter, ProbeResult, ProbeSpec, RunRecord, Task } from "./adapters/types";
import { serve as realServe, type Served } from "./serve";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const tasksDir = join(root, "tasks");
const sh = promisify(execFile);

export interface Deps {
  probes: (url: string, dir: string, probes: ProbeSpec[], databaseUrl: string, task: Task) => Promise<ProbeResult[]>;
  judge: (url: string, dir: string, task: Task) => Promise<number>;
  serve?: (dir: string) => Promise<Served>;
  journeys?: (task: Task, url: string) => Promise<{ id: string; pass: boolean }[]>;
}

export const loadTask = (id: string): Task => {
  const t = parse(readFileSync(join(tasksDir, id, "task.yaml"), "utf8")) as Task;
  return { ...t, timeoutMin: t.timeoutMin ?? 45 };
};
export const loadTasks = (ids?: string[]): Task[] => {
  const all = ids ?? readdirSync(tasksDir).filter((d) => !d.startsWith("_")).sort();
  return all.map(loadTask);
};

/** Run the task's journeys with Playwright's JSON reporter; journey id = test title. */
async function playwrightJourneys(task: Task, url: string): Promise<{ id: string; pass: boolean }[]> {
  let stdout = "";
  try {
    ({ stdout } = await sh("pnpm", ["exec", "playwright", "test", join("tasks", task.id, task.journeys), "--reporter=json"], { cwd: root, env: { ...process.env, BASE_URL: url }, maxBuffer: 100_000_000 }));
  } catch (e: any) { stdout = e.stdout ?? ""; } // exit 1 = some journeys failed; the report is still on stdout
  const found: { id: string; pass: boolean }[] = [];
  const walk = (s: any) => { for (const sp of s.specs ?? []) found.push({ id: sp.title, pass: sp.ok === true }); for (const c of s.suites ?? []) walk(c); };
  try { walk(JSON.parse(stdout)); } catch { /* no report = no journeys passed */ }
  return found;
}

const rate = (xs: { pass: boolean }[]) => (xs.length ? xs.filter((x) => x.pass).length / xs.length : 0);

export async function runTask(task: Task, adapter: BuilderAdapter, opts: { run: number; outDir: string }, deps: Deps): Promise<RunRecord> {
  const model = process.env.EVAL_MODEL ?? "claude-sonnet-5";
  const workdir = mkdtempSync(join(tmpdir(), `apps-${task.id}-`));
  cpSync(join(root, "fixtures", task.fixture), workdir, { recursive: true, filter: (s) => !/(^|\/)(node_modules|\.next)(\/|$)/.test(s) });
  const contractSrc = join(tasksDir, task.id, "contract.json");
  const contract = readFileSync(contractSrc);
  writeFileSync(join(workdir, "contract.json"), contract);
  if (existsSync(join(tasksDir, task.id, "attachments"))) cpSync(join(tasksDir, task.id, "attachments"), join(workdir, "attachments"), { recursive: true });
  const git = (...a: string[]) => sh("git", ["-c", "user.name=eval", "-c", "user.email=eval@localhost", ...a], { cwd: workdir });
  await git("init", "-q"); await git("add", "-A"); await git("commit", "-q", "-m", "fixture");

  const ac = new AbortController();
  let timer: NodeJS.Timeout | undefined;
  const timedOut = new Promise<"timeout">((r) => { timer = setTimeout(() => { ac.abort(); r("timeout"); }, task.timeoutMin * 60_000); });
  const base: Pick<RunRecord, "builder" | "model" | "taskId" | "run" | "journeys" | "probes" | "judge" | "score" | "passed" | "overRunCap"> = { builder: adapter.name, model, taskId: task.id, run: opts.run, journeys: [], probes: [], judge: 0, score: 0, passed: false, overRunCap: false };
  let rec: RunRecord;
  let serveLog = "";
  const t0 = Date.now();
  try {
    const built = await Promise.race([adapter.build(task, workdir, ac.signal), timedOut]);
    if (built === "timeout") {
      rec = { ...base, dir: workdir, tokensIn: 0, tokensOut: 0, dollars: 0, minutes: (Date.now() - t0) / 60_000, fixLoops: 0, status: "timeout", log: "" };
    } else {
      rec = { ...base, ...built, overRunCap: built.dollars > RUN_CAP_USD };
      const now = existsSync(join(workdir, "contract.json")) ? readFileSync(join(workdir, "contract.json")) : null;
      if (built.status !== "done") rec.reason = `build ${built.status}`;
      else if (!now || !now.equals(contract)) { rec.status = "contract_modified"; rec.reason = "contract.json was changed or deleted"; }
      else {
        const s = await (deps.serve ?? realServe)(workdir);
        serveLog = s.buildLog;
        try {
          if (!s.built) { rec.status = "serve_failed"; rec.reason = "app did not build or start"; }
          else {
            rec.journeys = await (deps.journeys ?? playwrightJourneys)(task, s.url);
            rec.probes = await deps.probes(s.url, workdir, task.probes, s.databaseUrl, task);
            rec.judge = await deps.judge(s.url, workdir, task);
            rec.score = 0.6 * rate(rec.journeys) + 0.25 * rate(rec.probes) + 0.15 * rec.judge;
            rec.passed = rate(rec.journeys) === 1 && rate(rec.probes) === 1; // the judge never decides pass/fail
          }
        } finally { await s.stop(); }
      }
    }
  } catch (e) {
    rec = { ...base, dir: workdir, tokensIn: 0, tokensOut: 0, dollars: 0, minutes: (Date.now() - t0) / 60_000, fixLoops: 0, status: ac.signal.aborted ? "timeout" : "error", log: "", reason: String(e) };
  } finally { clearTimeout(timer); }

  mkdirSync(opts.outDir, { recursive: true });
  const stem = join(opts.outDir, `${task.id}-run${opts.run}`);
  const { log, ...meta } = rec;
  writeFileSync(`${stem}.json`, JSON.stringify(meta, null, 2));
  writeFileSync(`${stem}.build.log`, log);
  writeFileSync(`${stem}.serve.log`, serveLog);
  return rec;
}

export async function runSuite(adapter: BuilderAdapter, opts: { tasks?: string[]; runs?: number; budgetUsd?: number; resultsRoot?: string }, deps: Deps): Promise<string> {
  const outDir = join(opts.resultsRoot ?? join(root, "results"), new Date().toISOString().replace(/[:.]/g, "-"));
  const budget = opts.budgetUsd ?? 400;
  let spent = 0;
  for (const task of loadTasks(opts.tasks))
    for (let run = 1; run <= (opts.runs ?? 3); run++) {
      if (spent >= budget) return outDir;
      spent += (await runTask(task, adapter, { run, outDir }, deps)).dollars;
    }
  return outDir;
}
