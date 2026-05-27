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

const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');

function parseArgs(argv) {
  const args = { mode: 'register', buildId: null, agentName: null };
  for (const a of argv.slice(2)) {
    if (a.startsWith('--mode=')) args.mode = a.split('=')[1];
    else if (a.startsWith('--build-id=')) args.buildId = a.split('=')[1];
    else if (a.startsWith('--agent-name=')) args.agentName = a.split('=')[1];
  }
  return args;
}

function _agentsRoot() {
  return process.env.ISOL8_AGENTS_DIR || path.join(os.homedir(), '.openclaw', 'agents');
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

function _buildCronJob({ buildId, reqId, n, agentName, schedule, tz, pipeline }) {
  const name = `${buildId}-${reqId}-${n}`;
  return {
    id: crypto.randomUUID(),
    agentId: agentName,
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

function _loadManifest(agentName) {
  const manifestPath = path.join(_agentsRoot(), agentName, 'build-manifest.json');
  if (!fs.existsSync(manifestPath)) return null;
  try { return JSON.parse(fs.readFileSync(manifestPath, 'utf8')); } catch (e) { return null; }
}

function _workspaceHasFile(agentName, relPath) {
  const full = path.join(_agentsRoot(), agentName, 'workspace', relPath);
  return fs.existsSync(full);
}

function _updateTriggersJson(agentName, registeredEntries) {
  // registeredEntries: [{req_id, kind, registered_as}]
  const triggersPath = path.join(_agentsRoot(), agentName, 'workspace', 'triggers.json');
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

function register({ buildId, agentName }) {
  if (!buildId || !agentName) {
    return { success: false, error: 'build_id_or_agent_name_missing', registered: [] };
  }
  const manifest = _loadManifest(agentName);
  if (!manifest) {
    return { success: false, error: 'manifest_not_found', agent_name: agentName, registered: [] };
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
      if (!_workspaceHasFile(agentName, t.pipeline)) {
        errors.push({ req_id: reqId, kind: 'cron', error: `pipeline file not in workspace: ${t.pipeline}` });
        break;
      }
      const job = _buildCronJob({
        buildId, reqId, n, agentName,
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
      if (!_workspaceHasFile(agentName, t.pipeline)) {
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
        if (!_workspaceHasFile(agentName, t.pipeline)) {
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
      agent_name: agentName,
      registered,
      errors,
      message: 'register-triggers aborted at first failure; partially-registered entries preserved for rollback'
    };
  }

  _updateTriggersJson(agentName, registered);

  return {
    success: true,
    build_id: buildId,
    agent_name: agentName,
    registered,
    cron_registered_count: registered.filter(r => r.kind === 'cron').length,
    hook_asserted_count: registered.filter(r => r.kind === 'hook').length,
    agent_call_noted_count: registered.filter(r => r.kind === 'agent-call').length
  };
}

function deregister({ buildId, agentName }) {
  if (!buildId || !agentName) {
    return { success: false, error: 'build_id_or_agent_name_missing' };
  }
  const store = _loadCronStore();
  const before = store.jobs.length;
  const namePrefix = `${buildId}-`;
  store.jobs = store.jobs.filter(j => !(j.agentId === agentName && typeof j.name === 'string' && j.name.startsWith(namePrefix)));
  const removed = before - store.jobs.length;
  if (removed > 0) {
    _writeCronStore(store);
  }
  return {
    success: true,
    build_id: buildId,
    agent_name: agentName,
    cron_removed: removed
  };
}

function main() {
  const args = parseArgs(process.argv);
  let result;
  if (args.mode === 'register') {
    result = register({ buildId: args.buildId, agentName: args.agentName });
  } else if (args.mode === 'deregister') {
    result = deregister({ buildId: args.buildId, agentName: args.agentName });
  } else {
    result = { success: false, error: `unknown mode: ${args.mode}` };
  }
  process.stdout.write(JSON.stringify(result));
  if (!result.success) process.exit(1);
}

if (require.main === module) main();

module.exports = { register, deregister, _buildCronJob, _loadCronStore };
