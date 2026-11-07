import { spawn, execFile } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { join } from "node:path";
import { promisify } from "node:util";
import { parse, stringify } from "yaml";

const sh = promisify(execFile);
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const STEP_MS = 10 * 60_000;
const START_MS = 90_000;

export interface Served { url: string; databaseUrl: string; stop(): Promise<void>; buildLog: string; built: boolean }

async function freePort(): Promise<number> {
  return new Promise((ok, fail) => {
    const s = createServer().once("error", fail).listen(0, "127.0.0.1", () => {
      const { port } = s.address() as { port: number };
      s.close(() => ok(port));
    });
  });
}

/** Run a command to completion; returns exit code and combined output. Never throws. */
function run(cmd: string, args: string[], cwd: string, env: NodeJS.ProcessEnv): Promise<{ code: number; out: string }> {
  return new Promise((done) => {
    let out = "";
    const p = spawn(cmd, args, { cwd, env, detached: true });
    const kill = () => { try { process.kill(-p.pid!, "SIGKILL"); } catch { /* gone */ } };
    const t = setTimeout(() => { out += `\n[timeout after ${STEP_MS / 1000}s]\n`; kill(); }, STEP_MS);
    p.stdout.on("data", (d) => (out += d));
    p.stderr.on("data", (d) => (out += d));
    p.on("error", (e) => { clearTimeout(t); done({ code: 1, out: out + String(e) }); });
    p.on("close", (code) => { clearTimeout(t); done({ code: code ?? 1, out }); });
  });
}

const BUILD_ALLOW = ["esbuild", "sharp", "@tailwindcss/oxide", "unrs-resolver"];
/**
 * Make `dir` its own pnpm workspace root with a build-script allowlist (pnpm 12 `allowBuilds`, older `onlyBuiltDependencies`).
 * pnpm 12 fails installs that leave dependency build scripts unapproved; a yaml here also stops the monorepo workspace from adopting the dir.
 * Authoritative: builder output is untrusted and installs run on the host, so whatever the builder wrote under those keys
 * (or dangerouslyAllowAllBuilds) is dropped; their other settings are kept.
 */
export function writeBuildAllowlist(dir: string) {
  const f = join(dir, "pnpm-workspace.yaml");
  let cfg: Record<string, unknown> = {};
  try { const y = parse(existsSync(f) ? readFileSync(f, "utf8") : ""); if (y && typeof y === "object" && !Array.isArray(y)) cfg = y; } catch { /* unreadable yaml: start clean */ }
  for (const k of Object.keys(cfg)) if (/^(allowBuilds|onlyBuiltDependencies|dangerouslyAllowAllBuilds|neverBuiltDependencies|ignoredBuiltDependencies)$/i.test(k.replace(/-/g, ""))) delete cfg[k];
  cfg.allowBuilds = Object.fromEntries(BUILD_ALLOW.map((n) => [n, true]));
  cfg.onlyBuiltDependencies = [...BUILD_ALLOW];
  writeFileSync(f, stringify(cfg));
}

export interface ServeOpts { before?: { fixtureDir: string; seedSql: string } }

export async function serve(dir: string, opts: ServeOpts = {}): Promise<Served> {
  let log = "";
  let app: ReturnType<typeof spawn> | undefined;
  let container = "";
  let stopped = false;

  const stop = async () => {
    if (stopped) return;
    stopped = true;
    if (app?.pid) try { process.kill(-app.pid, "SIGKILL"); } catch { /* gone */ }
    if (container) await sh("docker", ["rm", "-f", "-v", container], { timeout: 30_000 }).catch(() => {});
  };
  const fail = async (msg: string): Promise<Served> => {
    log += `\n[serve] ${msg}\n`;
    await stop();
    return { url: "", databaseUrl: "", stop, buildLog: log, built: false };
  };
  const step = async (title: string, cmd: string, args: string[], env: NodeJS.ProcessEnv, cwd = dir) => {
    const r = await run(cmd, args, cwd, env);
    log += `\n$ ${title}\n${r.out}`;
    return r.code === 0;
  };

  try {
    // Throwaway Postgres on a random loopback port.
    let databaseUrl: string;
    try {
      container = (await sh("docker", ["run", "--rm", "-d", "--label", "lightbulb-eval=1", "-e", "POSTGRES_PASSWORD=eval", "-p", "127.0.0.1::5432", "postgres:18"], { timeout: 5 * 60_000 })).stdout.trim();
      const hostPort = (await sh("docker", ["port", container, "5432/tcp"], { timeout: 30_000 })).stdout.trim().split("\n")[0].split(":").pop();
      databaseUrl = `postgres://postgres:eval@127.0.0.1:${hostPort}/postgres`;
    } catch (e) {
      return fail(`postgres start failed: ${e}`);
    }
    // -h 127.0.0.1: the init-time temp server has no TCP listener, so this waits for the real one.
    let ready = false;
    for (let i = 0; i < 60 && !ready; i++) {
      ready = await sh("docker", ["exec", container, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"], { timeout: 30_000 }).then(() => true, () => false);
      if (!ready) await sleep(500);
    }
    if (!ready) return fail("postgres never became ready");

    const env = { ...process.env, DATABASE_URL: databaseUrl, CRON_SECRET: "eval-cron-secret", NODE_ENV: "production", NO_COLOR: "1" };
    const buildEnv = { ...env, NODE_ENV: "development" }; // install must keep devDependencies (typescript etc.)
    // Seed the fixture's schema before the builder's migrations run, so a destructive migration has data to destroy.
    if (opts.before) {
      const { fixtureDir, seedSql } = opts.before;
      if (existsSync(join(fixtureDir, "drizzle.config.ts")) &&
          !((writeBuildAllowlist(fixtureDir), await step("fixture: pnpm install", "pnpm", ["install"], buildEnv, fixtureDir)) &&
            (await step("fixture: pnpm drizzle-kit migrate", "pnpm", ["drizzle-kit", "migrate"], env, fixtureDir)))) return fail("fixture migrate failed");
      if (!(await step("fixture: seed", "docker", ["exec", container, "psql", "-U", "postgres", "-v", "ON_ERROR_STOP=1", "-c", seedSql], env))) return fail("fixture seed failed");
    }
    writeBuildAllowlist(dir); // the yaml makes dir its own workspace root, so no --ignore-workspace (which would also ignore the yaml)
    if (!(await step("pnpm install --frozen-lockfile", "pnpm", ["install", "--frozen-lockfile"], buildEnv)) &&
        !(await step("pnpm install", "pnpm", ["install"], buildEnv))) return fail("install failed");
    if (existsSync(join(dir, "drizzle.config.ts")) &&
        !(await step("pnpm drizzle-kit migrate", "pnpm", ["drizzle-kit", "migrate"], env))) return fail("migrate failed");
    if (!(await step("pnpm build", "pnpm", ["build"], env))) return fail("build failed");

    const port = await freePort();
    app = spawn("pnpm", ["start", "-p", String(port)], { cwd: dir, env: { ...env, PORT: String(port) }, detached: true });
    app.stdout!.on("data", (d) => (log += d));
    app.stderr!.on("data", (d) => (log += d));
    let exited = false;
    app.on("close", () => (exited = true));
    app.on("error", (e) => { log += `\n[serve] start failed: ${e}\n`; exited = true; });
    log += `\n$ pnpm start -p ${port}\n`;

    // `next start` without -H builds request URLs (req.nextUrl.origin) on localhost; a browser on 127.0.0.1 sends a different Origin,
    // so a same-origin check that is correct on any real host would refuse every write. Serve on the origin Next reports.
    const url = `http://localhost:${port}`;
    const deadline = Date.now() + START_MS;
    while (Date.now() < deadline && !exited) {
      if (await fetch(url, { signal: AbortSignal.timeout(2000) }).then(() => true, () => false)) {
        return { url, databaseUrl, stop, buildLog: log, built: true };
      }
      await sleep(500);
    }
    return fail(exited ? "app exited before answering /" : `timeout: app did not answer / within ${START_MS / 1000}s`);
  } catch (e) {
    return fail(`unexpected error: ${e}`);
  }
}
