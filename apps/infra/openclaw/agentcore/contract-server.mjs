/**
 * AgentCore Runtime HTTP contract -> OpenClaw gateway.
 *
 * AgentCore Runtime requires a server on 0.0.0.0:8080 speaking two endpoints:
 *   GET  /ping        -> {"status":"Healthy"|"HealthyBusy","time_of_last_update":<epoch>}
 *   POST /invocations -> the agent call
 *
 * OpenClaw's gateway speaks plain HTTP on 18789 (the lightbulb-channel plugin's routes:
 * /plugins/lightbulb-channel/inbound/<agentId>, /rpc, run abort/steer, hooks). So this process
 * is a thin envelope translator plus the gateway's supervisor: it owns PID 1 semantics inside
 * the microVM, starts the gateway, and answers /ping honestly while the gateway is still
 * booting — a failed /ping means AgentCore discards the microVM and pays a full cold start, so
 * "not ready yet" must be 200-with-busy or a clean 503, never a crash.
 *
 * Lifecycle: the gateway is spawned on the FIRST /invocations, not at container start — see
 * ensureGateway(). Until then /ping answers 200 HealthyBusy, which is what a pooled instance
 * that has never been assigned a session must look like.
 *
 * Scope: P3 proof of life. The /ws relay pipe, ready/ending callbacks and prepare-stop are not
 * here yet; see the spec's P3/P5.
 */
import { createServer } from "node:http";
import { spawn } from "node:child_process";
import { request as httpRequest } from "node:http";

const PORT = Number(process.env.AGENTCORE_PORT ?? 8080);
const GATEWAY_PORT = Number(process.env.OPENCLAW_GATEWAY_PORT ?? 18789);
const GATEWAY_CMD = process.env.OPENCLAW_GATEWAY_CMD ?? "node";
const GATEWAY_ARGS = (process.env.OPENCLAW_GATEWAY_ARGS ??
  `--max-old-space-size=1024 /app/openclaw.mjs gateway --port ${GATEWAY_PORT} --bind lan`)
  .split(/\s+/).filter(Boolean);   // filter: an empty env var must mean NO args, not one blank arg

let gatewayReady = false;
let gatewayExited = null;
let lastReadyCheck = 0;
const startedAt = Date.now();
let gatewayStarted = false;

function log(...a) { console.log("[contract]", ...a); }

// ---- supervise the gateway -------------------------------------------------
// A dead child must NEVER leave this process alive and answering /ping. AgentCore keeps a
// warm microVM and keeps routing to it, so a container that survives its own gateway becomes
// a zombie: /ping says HealthyBusy forever and every invocation lands on a VM that can never
// serve one. Observed 2026-09-16 — a boot-chain failure left a microVM answering for 8+
// minutes. So: bounded restarts, then exit non-zero and let AgentCore discard the microVM.
const MAX_RESTARTS = Number(process.env.OPENCLAW_MAX_RESTARTS ?? 2);
let restarts = 0;
let child = null;

// The launcher builds ~/.openclaw from the session's EFS mount (which only exists once a
// session is assigned — see boot-layout.sh), then execs the gateway chain.
const LAUNCHER = process.env.OPENCLAW_GATEWAY_LAUNCHER ?? "/opt/agentcore/boot-layout.sh";

function spawnGateway() {
  gatewayStarted = true;
  child = spawn(LAUNCHER, [GATEWAY_CMD, ...GATEWAY_ARGS], { stdio: ["ignore", "inherit", "inherit"], env: process.env });
  log(`spawned gateway pid=${child.pid} (attempt ${restarts + 1}/${MAX_RESTARTS + 1}): ${LAUNCHER} ${GATEWAY_CMD} ${GATEWAY_ARGS.join(" ")}`);
  child.on("exit", (code, signal) => {
    gatewayReady = false;
    const uptime = ((Date.now() - startedAt) / 1000).toFixed(1);
    log(`gateway EXITED code=${code} signal=${signal} after ${uptime}s`);
    if (restarts < MAX_RESTARTS) {
      restarts += 1;
      log(`restarting gateway in 2s (${restarts}/${MAX_RESTARTS})`);
      setTimeout(spawnGateway, 2000);
      return;
    }
    gatewayExited = { code, signal, at: new Date().toISOString(), restarts };
    log(`FATAL: gateway exited ${restarts + 1} times; exiting so AgentCore discards this microVM`);
    process.exit(code === 0 ? 0 : 1);
  });
}

// The gateway starts on the FIRST invocation, never at container start. AgentCore keeps a
// warm pool of pre-started instances per runtime version (observed: ten, within seconds of
// READY, before any invoke) and each runs this CMD. A gateway that boots at container start
// therefore boots ten times per version: ten litestream restore+replicate loops on one S3
// prefix and one EFS access point — the two-writers corruption reproduced on 2026-09-16.
// /invocations is the only signal that a session has actually been assigned to this instance.
function ensureGateway() {
  if (!gatewayStarted) {
    log(`first invocation after ${((Date.now() - startedAt) / 1000).toFixed(1)}s idle in the pool; starting gateway`);
    spawnGateway();
  }
}

/** Probe the gateway's own /healthz. Cheap, and cached for a second. */
function probeGateway() {
  return new Promise((resolve) => {
    const req = httpRequest(
      { host: "127.0.0.1", port: GATEWAY_PORT, path: "/healthz", method: "GET", timeout: 3000 },
      (res) => { res.resume(); resolve(res.statusCode >= 200 && res.statusCode < 500); },
    );
    req.on("error", () => resolve(false));
    req.on("timeout", () => { req.destroy(); resolve(false); });
    req.end();
  });
}

setInterval(async () => {
  if (gatewayExited || !gatewayStarted) return;
  const up = await probeGateway();
  if (up && !gatewayReady) log(`gateway READY after ${((Date.now() - startedAt) / 1000).toFixed(1)}s`);
  gatewayReady = up;
  lastReadyCheck = Date.now();
}, 1000).unref?.();

// ---- the AgentCore contract ------------------------------------------------
function send(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, { "content-type": "application/json", "content-length": Buffer.byteLength(body) });
  res.end(body);
}

/** Proxy an envelope to the gateway. Envelope: {path, method?, headers?, body?}. */
function proxyToGateway(envelope) {
  return new Promise((resolve) => {
    const payload = envelope.body === undefined ? null
      : (typeof envelope.body === "string" ? envelope.body : JSON.stringify(envelope.body));
    const req = httpRequest({
      host: "127.0.0.1",
      port: GATEWAY_PORT,
      path: envelope.path ?? "/healthz",
      method: envelope.method ?? "GET",
      timeout: 120000,
      headers: {
        "content-type": "application/json",
        ...(payload ? { "content-length": Buffer.byteLength(payload) } : {}),
        ...(envelope.headers ?? {}),
      },
    }, (res) => {
      let data = "";
      res.on("data", (c) => { data += c; });
      res.on("end", () => resolve({ status: res.statusCode, headers: res.headers, body: data }));
    });
    req.on("error", (e) => resolve({ status: 502, error: String(e) }));
    req.on("timeout", () => { req.destroy(); resolve({ status: 504, error: "gateway timeout" }); });
    if (payload) req.write(payload);
    req.end();
  });
}

createServer(async (req, res) => {
  if (req.method === "GET" && req.url === "/ping") {
    // HealthyBusy keeps the session alive without claiming readiness for new work.
    const status = gatewayReady ? "Healthy" : "HealthyBusy";
    return send(res, 200, { status, time_of_last_update: Math.floor(Date.now() / 1000) });
  }

  if (req.method === "POST" && req.url === "/invocations") {
    let raw = "";
    for await (const chunk of req) raw += chunk;
    let envelope;
    try { envelope = raw ? JSON.parse(raw) : {}; }
    catch { return send(res, 400, { error: "body is not JSON", raw: raw.slice(0, 200) }); }

    // A diagnostic escape hatch for P3 bring-up: report what the microVM looks like.
    if (envelope.diagnostic) {
      const { execSync } = await import("node:child_process");
      const sh = (c) => { try { return execSync(c, { encoding: "utf8", timeout: 20000 }); } catch (e) { return `ERR ${e.message}`; } };
      return send(res, 200, {
        uptime_s: +((Date.now() - startedAt) / 1000).toFixed(1),
        gatewayReady, gatewayExited, lastReadyCheck,
        df: sh("df -hT / /tmp /mnt /mnt/* 2>&1"),
        mounts: sh("grep -E ' /(mnt|home|tmp)' /proc/self/mountinfo 2>&1"),
        openclaw_dir: sh("ls -la /home/node/.openclaw 2>&1 | head -30"),
        gateway_listening: sh(`(ss -ltnp 2>/dev/null || netstat -ltn 2>/dev/null) | grep -E '${GATEWAY_PORT}|8080' 2>&1`),
        cmd: envelope.cmd ? sh(envelope.cmd) : undefined,
      });
    }

    if (gatewayExited) return send(res, 503, { error: "gateway exited", gatewayExited });
    ensureGateway();
    if (!gatewayReady) return send(res, 503, { error: "gateway still booting", uptime_s: +((Date.now() - startedAt) / 1000).toFixed(1) });
    return send(res, 200, await proxyToGateway(envelope));
  }

  send(res, 404, { error: "not found", want: ["GET /ping", "POST /invocations"] });
}).listen(PORT, "0.0.0.0", () => log(`contract server on 0.0.0.0:${PORT} -> gateway ${GATEWAY_PORT}`));
