#!/usr/bin/env node
// atlas-trigger-registrar.js
//
// Registers triggers for a freshly-built Atlas agent. Two modes:
//   --mode=register   — write cron rows + assert hook pipelines exist
//   --mode=deregister — remove every cron row whose name starts with {build_id}-
//
// Cron mechanism (per spec Q4 resolution): direct JSON write to the
// configured cron store. Matches the persisted shape in
// catalog_slice.py:204-228 and the write mechanism in
// config_patcher.append_cron_jobs.
//
// Hook mechanism (per spec Q1 resolution): no per-route CLI exists; hooks
// are framework-routed by session-key prefix. This registrar's hook branch
// is a no-op assert — it verifies the trigger's declared .lobster pipeline
// is present in the deployed agent's workspace/ dir.
//
// Channel kind is NOT supported in v1 (spec Q2) — flagged as an error.
//
// Cron-job naming (per spec Q3): name = `{build_id}-{req_id}-{n}` (1-indexed
// across the build's triggers list for that req_id); id is a fresh UUID v4.
//
// `--agent-id` is the id the BACKEND BRIDGE assigned at register (the
// Paperclip-minted uuid) — never a name Atlas picked. A cron row keyed to
// anything else addresses an agent that does not exist and silently never
// fires. Everything else here (manifest, pipeline files) is read out of the
// build's staging workspace, which is keyed by the build id.

const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');

// Resolve the staging workspace through the generator's own derivation rather
// than a second copy of it. Two copies that drift is exactly how the build
// pipeline and the backend bridge ended up reading different directories.
// Both files are staged into this workspace together by the Atlas seeder;
// the require is file-relative, so cwd doesn't matter.
const { stagingWorkspaceDir: _stagingWorkspaceDir } = require('./atlas-skeleton-generator.js');

function parseArgs(argv) {
  const args = { mode: 'register', buildId: null, agentId: null };
  for (const a of argv.slice(2)) {
    if (a.startsWith('--mode=')) args.mode = a.split('=')[1];
    else if (a.startsWith('--build-id=')) args.buildId = a.split('=')[1];
    else if (a.startsWith('--agent-id=')) args.agentId = a.split('=')[1];
  }
  return args;
}

function _cronStorePath() {
  return process.env.ATLAS_CRON_STORE || path.join(os.homedir(), '.openclaw', 'cron', 'jobs.json');
}

function _loadCronStore() {
  const p = _cronStorePath();
  if (!fs.existsSync(p)) return { version: 1, jobs: [] };
  try {
    const data = JSON.parse(fs.readFileSync(p, 'utf8'));
    if (!data || typeof data !== 'object') return { version: 1, jobs: [] };
    if (!Array.isArray(data.jobs)) data.jobs = [];
    if (!data.version) data.version = 1;
    return data;
  } catch (e) {
    return { version: 1, jobs: [] };
  }
}

function _writeCronStore(store) {
  const p = _cronStorePath();
  fs.mkdirSync(path.dirname(p), { recursive: true });
  // Atomic temp+rename to avoid corruption on crash mid-write.
  const tmp = p + '.tmp-' + process.pid + '-' + Date.now();
  fs.writeFileSync(tmp, JSON.stringify(store, null, 2));
  fs.renameSync(tmp, p);
}

function _buildCronJob({ buildId, reqId, n, agentId, schedule, tz, pipeline }) {
  const name = `${buildId}-${reqId}-${n}`;
  return {
    id: crypto.randomUUID(),
    agentId,
    name,
    enabled: true,
    schedule: { kind: 'cron', expr: schedule, tz: tz || null },
    sessionTarget: 'isolated',
    wakeMode: 'light-context',
    payload: { kind: 'lobster', message: `Run ${pipeline}` },
    delivery: { mode: 'internal' },
    createdAtMs: Date.now()
  };
}

function _loadManifest(buildId) {
  const manifestPath = path.join(_stagingWorkspaceDir(buildId), 'build-manifest.json');
  if (!fs.existsSync(manifestPath)) return null;
  try { return JSON.parse(fs.readFileSync(manifestPath, 'utf8')); } catch (e) { return null; }
}

// Whether `relPath` names a .lobster pipeline that actually lives inside THIS
// build's staging workspace.
//
// `relPath` comes from the build manifest's triggers[], which the agent loop
// authors — so it is untrusted input reaching a filesystem path. `path.join`
// happily resolves `..`, so a pipeline like `../../other-build/workspace/x.lobster`
// would satisfy a bare existsSync outside this workspace and then get baked into
// a cron row's `Run <pipeline>` payload. Confine it: relative, `.lobster`, and
// still under workspace/ after the symlinks are resolved.
function _workspaceHasFile(buildId, relPath) {
  if (typeof relPath !== 'string' || !relPath.endsWith('.lobster') || path.isAbsolute(relPath)) {
    return false;
  }
  // resolve() both sides: LIGHTBULB_AGENTS_DIR may be relative, and comparing a
  // resolved path against an unresolved root never matches.
  const root = path.resolve(_stagingWorkspaceDir(buildId), 'workspace');
  const full = path.resolve(root, relPath);
  if (full !== root && !full.startsWith(root + path.sep)) return false;
  if (!fs.existsSync(full)) return false;
  // realpath AFTER the existence check (it throws on a missing path): a symlink
  // inside the workspace must not point out of it either.
  try {
    const realRoot = fs.realpathSync(root);
    const realFull = fs.realpathSync(full);
    return realFull.startsWith(realRoot + path.sep);
  } catch (e) {
    return false;
  }
}

function _updateTriggersJson(buildId, registeredEntries) {
  // registeredEntries: [{req_id, kind, registered_as}]
  const triggersPath = path.join(_stagingWorkspaceDir(buildId), 'workspace', 'triggers.json');
  if (!fs.existsSync(triggersPath)) return;
  let data;
  try { data = JSON.parse(fs.readFileSync(triggersPath, 'utf8')); } catch (e) { return; }
  if (!Array.isArray(data.triggers)) return;
  // Match by (req_id, kind) — there can be multiple triggers per req of the
  // same kind, so consume entries in order.
  const queue = registeredEntries.slice();
  for (const t of data.triggers) {
    const idx = queue.findIndex(r => r.req_id === t.req_id && r.kind === t.kind);
    if (idx === -1) continue;
    t.registered_as = queue[idx].registered_as;
    queue.splice(idx, 1);
  }
  fs.writeFileSync(triggersPath, JSON.stringify(data, null, 2));
}

function register({ buildId, agentId }) {
  if (!buildId || !agentId) {
    return { success: false, error: 'build_id_or_agent_id_missing', registered: [] };
  }
  const manifest = _loadManifest(buildId);
  if (!manifest) {
    return { success: false, error: 'manifest_not_found', agent_id: agentId, registered: [] };
  }
  const triggers = Array.isArray(manifest.triggers) ? manifest.triggers : [];

  const registered = [];
  const errors = [];

  // Per-req_id 1-indexed counter for the name suffix.
  const perReqCounter = {};

  for (const t of triggers) {
    const reqId = t.req_id;
    perReqCounter[reqId] = (perReqCounter[reqId] || 0) + 1;
    const n = perReqCounter[reqId];

    if (t.kind === 'cron') {
      if (!t.schedule || !t.pipeline) {
        errors.push({ req_id: reqId, kind: 'cron', error: 'missing schedule or pipeline' });
        break;
      }
      if (!_workspaceHasFile(buildId, t.pipeline)) {
        errors.push({ req_id: reqId, kind: 'cron', error: `pipeline file not in workspace: ${t.pipeline}` });
        break;
      }
      const job = _buildCronJob({
        buildId, reqId, n, agentId,
        schedule: t.schedule, tz: t.tz, pipeline: t.pipeline
      });
      const store = _loadCronStore();
      store.jobs.push(job);
      _writeCronStore(store);
      registered.push({ req_id: reqId, kind: 'cron', registered_as: job.name, job_id: job.id });
    } else if (t.kind === 'hook') {
      // No CLI to call — hooks are framework-routed. Just assert the pipeline exists.
      if (!t.pipeline || !t.pipeline.endsWith('.lobster')) {
        errors.push({ req_id: reqId, kind: 'hook', error: 'hook requires .lobster pipeline' });
        break;
      }
      if (!_workspaceHasFile(buildId, t.pipeline)) {
        errors.push({ req_id: reqId, kind: 'hook', error: `pipeline file not in workspace: ${t.pipeline}` });
        break;
      }
      registered.push({ req_id: reqId, kind: 'hook', registered_as: `framework-routed:${t.route}` });
    } else if (t.kind === 'agent-call') {
      // No registration needed. If pipeline is non-null, assert it exists.
      if (t.pipeline !== null && t.pipeline !== undefined) {
        if (!t.pipeline.endsWith('.lobster')) {
          errors.push({ req_id: reqId, kind: 'agent-call', error: 'agent-call pipeline must be null or .lobster' });
          break;
        }
        if (!_workspaceHasFile(buildId, t.pipeline)) {
          errors.push({ req_id: reqId, kind: 'agent-call', error: `pipeline file not in workspace: ${t.pipeline}` });
          break;
        }
      }
      registered.push({ req_id: reqId, kind: 'agent-call', registered_as: 'no-registration-required' });
    } else {
      errors.push({ req_id: reqId, kind: t.kind, error: `unsupported trigger kind: ${t.kind}` });
      break;
    }
  }

  if (errors.length > 0) {
    return {
      success: false,
      build_id: buildId,
      agent_id: agentId,
      registered,
      errors,
      message: 'register-triggers aborted at first failure; partially-registered entries preserved for rollback'
    };
  }

  _updateTriggersJson(buildId, registered);

  return {
    success: true,
    build_id: buildId,
    agent_id: agentId,
    registered,
    cron_registered_count: registered.filter(r => r.kind === 'cron').length,
    hook_asserted_count: registered.filter(r => r.kind === 'hook').length,
    agent_call_noted_count: registered.filter(r => r.kind === 'agent-call').length
  };
}

function deregister({ buildId, agentId }) {
  if (!buildId || !agentId) {
    return { success: false, error: 'build_id_or_agent_id_missing' };
  }
  const store = _loadCronStore();
  const before = store.jobs.length;
  const namePrefix = `${buildId}-`;
  store.jobs = store.jobs.filter(j => !(j.agentId === agentId && typeof j.name === 'string' && j.name.startsWith(namePrefix)));
  const removed = before - store.jobs.length;
  if (removed > 0) {
    _writeCronStore(store);
  }
  return {
    success: true,
    build_id: buildId,
    agent_id: agentId,
    cron_removed: removed
  };
}

function main() {
  const args = parseArgs(process.argv);
  let result;
  if (args.mode === 'register') {
    result = register({ buildId: args.buildId, agentId: args.agentId });
  } else if (args.mode === 'deregister') {
    result = deregister({ buildId: args.buildId, agentId: args.agentId });
  } else {
    result = { success: false, error: `unknown mode: ${args.mode}` };
  }
  process.stdout.write(JSON.stringify(result));
  if (!result.success) process.exit(1);
}

if (require.main === module) main();

module.exports = { register, deregister, _buildCronJob, _loadCronStore, _stagingWorkspaceDir, _workspaceHasFile };
