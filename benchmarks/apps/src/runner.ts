import { execFile } from "node:child_process";
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { parse } from "yaml";
import { RUN_CAP_USD } from "./adapters/claude-code";
import type { BuilderAdapter, BuildResult, ProbeResult, ProbeSpec, RunRecord, Task } from "./adapters/types";
import { serve as realServe, type Served, type ServeOpts } from "./serve";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const tasksDir = join(root, "tasks");
const sh = promisify(execFile);

export interface Deps {
  probes: (url: string, dir: string, probes: ProbeSpec[], databaseUrl: string, task: Task) => Promise<ProbeResult[]>;
  judge: (url: string, dir: string, task: Task) => Promise<{ score: number; reason: string }>;
  serve?: (dir: string, opts?: ServeOpts) => Promise<Served>;
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
export async function playwrightJourneys(task: Task, url: string): Promise<{ id: string; pass: boolean }[]> {
  let stdout = "";
  try {
    ({ stdout } = await sh("pnpm", ["exec", "playwright", "test", join("tasks", task.id, task.journeys), "--reporter=json", "--global-timeout=600000"], { cwd: root, env: { ...process.env, BASE_URL: url }, maxBuffer: 100_000_000 }));
  } catch (e: any) { stdout = e.stdout ?? ""; } // exit 1 = some journeys failed; the report is still on stdout
  try { return journeyResults(JSON.parse(stdout)); } catch { return []; } // no report = no journeys passed
}

/** A journey passes only if every test under that title ran and was expected; skipped is a failure. */
export function journeyResults(report: any): { id: string; pass: boolean; error?: string }[] {
  const found: { id: string; pass: boolean; error?: string }[] = [];
  const walk = (s: any) => {
    for (const sp of s.specs ?? []) {
      const pass = (sp.tests ?? []).length > 0 && sp.tests.every((t: any) => t.status === "expected" || t.status === "flaky");
      // keep the first failure message (ANSI stripped, first 400 chars) so a failed journey says why without a re-run
      const error = pass ? undefined : (sp.tests ?? []).flatMap((t: any) => t.results ?? []).map((r: any) => r.error?.message).find(Boolean)?.replace(/\u001b\[[0-9;]*m/g, "").slice(0, 400);
      found.push(error ? { id: sp.title, pass, error } : { id: sp.title, pass });
    }
    for (const c of s.suites ?? []) walk(c);
  };
  walk(report);
  return found;
}

const rate = (xs: { pass: boolean }[]) => (xs.length ? xs.filter((x) => x.pass).length / xs.length : 0);

export const GRACE_MS = 15_000;
const sleep = (ms: number) => new Promise<"grace">((r) => setTimeout(() => r("grace"), ms));

export async function runTask(task: Task, adapter: BuilderAdapter, opts: { run: number; outDir: string; keepWorkdir?: boolean }, deps: Deps): Promise<RunRecord> {
  const model = process.env.EVAL_MODEL ?? adapter.defaultModel ?? "claude-sonnet-5";
  const workdir = mkdtempSync(join(tmpdir(), `apps-${task.id}-`));
  let serveLog = "";
  let rec!: RunRecord;
  try {
    cpSync(join(root, "fixtures", task.fixture), workdir, { recursive: true, filter: (s) => !/(^|\/)(node_modules|\.next)(\/|$)/.test(s) });
    const contract = readFileSync(join(tasksDir, task.id, "contract.json"));
    writeFileSync(join(workdir, "contract.json"), contract);
    if (existsSync(join(tasksDir, task.id, "attachments"))) cpSync(join(tasksDir, task.id, "attachments"), join(workdir, "attachments"), { recursive: true });
    const git = (...a: string[]) => sh("git", ["-c", "user.name=eval", "-c", "user.email=eval@localhost", ...a], { cwd: workdir });
    await git("init", "-q"); await git("add", "-A"); await git("commit", "-q", "-m", "fixture");

    const ac = new AbortController();
    let timer: NodeJS.Timeout | undefined;
    const timedOut = new Promise<"timeout">((r) => { timer = setTimeout(() => { ac.abort(); r("timeout"); }, task.timeoutMin * 60_000); });
    const billing = adapter.billing ?? "api";
    const base: Pick<RunRecord, "builder" | "model" | "billing" | "taskId" | "run" | "journeys" | "probes" | "judge" | "score" | "passed" | "overRunCap" | "minutesToRunnable"> = { builder: adapter.name, model, billing, taskId: task.id, run: opts.run, journeys: [], probes: [], judge: 0, score: 0, passed: false, overRunCap: false, minutesToRunnable: null };
    const zero = { dir: workdir, tokensIn: 0, tokensOut: 0, dollars: 0, fixLoops: 0, log: "" };
    const t0 = Date.now();
    const mins = () => (Date.now() - t0) / 60_000;
    const buildP: Promise<BuildResult | { thrown: unknown }> = adapter.build(task, workdir, ac.signal).catch((thrown) => ({ thrown }));
    try {
      const first = await Promise.race([buildP, timedOut]);
      let built: BuildResult | { thrown: unknown } | "grace" = first as any;
      if (first === "timeout") built = await Promise.race([buildP, sleep(GRACE_MS)]); // the abort kills the builder, so it settles with its partial cost
      if (built === "grace" || "thrown" in built) {
        // no cost reported: unknown spend, the suite counts the per-run cap
        rec = { ...base, ...zero, minutes: mins(), status: first === "timeout" ? "timeout" : "error", costUnknown: billing === "api", reason: built === "grace" ? "builder did not stop after abort" : String(built.thrown) };
      } else {
        rec = { ...base, ...built, overRunCap: built.dollars > RUN_CAP_USD };
        if (first === "timeout") rec.status = "timeout";
        const now = existsSync(join(workdir, "contract.json")) ? readFileSync(join(workdir, "contract.json")) : null;
        if (rec.status !== "done") rec.reason = `build ${rec.status}`;
        else if (!now || !now.equals(contract)) { rec.status = "contract_modified"; rec.reason = "contract.json was changed or deleted"; }
        else {
          const seed = task.probes.find((p) => p.kind === "destructive-migration" && p.seed)?.seed;
          const s = await (deps.serve ?? realServe)(workdir, seed ? { before: { fixtureDir: join(root, "fixtures", task.fixture), seedSql: seed } } : undefined);
          serveLog = s.buildLog;
          try {
            if (!s.built) { rec.status = "serve_failed"; rec.reason = "app did not build or start"; }
            else {
              rec.minutesToRunnable = mins();
              rec.journeys = await (deps.journeys ?? playwrightJourneys)(task, s.url);
              // judge before probes: the sql-injection probe may break a vulnerable app
              const { score: j, reason } = await deps.judge(s.url, workdir, task);
              rec.judgeReason = reason;
              rec.probes = await deps.probes(s.url, workdir, task.probes, s.databaseUrl, task);
              rec.judge = Number.isNaN(j) ? 0 : j; // no judge score counts as 0 and is flagged
              if (Number.isNaN(j)) rec.judgeFailed = true;
              rec.score = 0.6 * rate(rec.journeys) + 0.25 * rate(rec.probes) + 0.15 * rec.judge;
              rec.passed = rate(rec.journeys) === 1 && rate(rec.probes) === 1; // the judge never decides pass/fail
            }
          } finally { await s.stop(); }
        }
      }
    } catch (e) {
      rec = { ...base, ...zero, minutes: mins(), status: ac.signal.aborted ? "timeout" : "error", costUnknown: billing === "api", reason: String(e) };
    } finally { clearTimeout(timer); }

    mkdirSync(opts.outDir, { recursive: true });
    const stem = join(opts.outDir, `${task.id}-run${opts.run}`);
    const { log, ...meta } = rec;
    writeFileSync(`${stem}.json`, JSON.stringify(meta, null, 2));
    writeFileSync(`${stem}.build.log`, log);
    writeFileSync(`${stem}.serve.log`, serveLog);
    return rec;
  } finally {
    if (!opts.keepWorkdir) rmSync(workdir, { recursive: true, force: true });
  }
}

export async function runSuite(adapter: BuilderAdapter, opts: { tasks?: string[]; runs?: number; budgetUsd?: number; keepWorkdir?: boolean; resultsRoot?: string; onStart?: (outDir: string) => Promise<void> | void }, deps: Deps): Promise<string> {
  const outDir = join(opts.resultsRoot ?? join(root, "results"), new Date().toISOString().replace(/[:.]/g, "-"));
  const budget = opts.budgetUsd ?? 400;
  let spent = 0;
  const tasks = loadTasks(opts.tasks);
  const runs = opts.runs ?? 3;
  mkdirSync(outDir, { recursive: true });
  await opts.onStart?.(outDir);
  writeFileSync(join(outDir, "suite.json"), JSON.stringify({ tasks: tasks.map((t) => t.id), runs })); // the report's denominator
  for (const task of tasks)
    for (let run = 1; run <= runs; run++) {
      if (spent >= budget) return outDir;
      const r = await runTask(task, adapter, { run, outDir, keepWorkdir: opts.keepWorkdir }, deps);
      if (r.billing === "subscription") continue; // subscription runs cost no dollars
      spent += r.costUnknown ? RUN_CAP_USD : r.dollars; // unknown cost counts the per-run cap so the suite cap stays conservative
    }
  return outDir;
}
