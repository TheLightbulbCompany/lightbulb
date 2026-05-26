// Trigger registrar — writes cron jobs into ~/.openclaw/cron/jobs.json
// (override via ATLAS_CRON_STORE env), asserts hook pipelines exist,
// emits per-trigger result lines + summary JSON.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');

function mkTmpDirs() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'atlas-reg-'));
  const agentsRoot = path.join(root, 'agents');
  const cronStore = path.join(root, 'cron', 'jobs.json');
  fs.mkdirSync(agentsRoot, { recursive: true });
  fs.mkdirSync(path.dirname(cronStore), { recursive: true });
  return { root, agentsRoot, cronStore };
}

function seedDeployedAgent(agentsRoot, agentName, manifest, workspaceFiles = {}) {
  const dest = path.join(agentsRoot, agentName);
  const workspace = path.join(dest, 'workspace');
  fs.mkdirSync(workspace, { recursive: true });
  fs.writeFileSync(path.join(dest, 'build-manifest.json'), JSON.stringify(manifest));
  for (const [rel, content] of Object.entries(workspaceFiles)) {
    fs.writeFileSync(path.join(workspace, rel), content);
  }
  return dest;
}

test('register writes a cron job entry to ATLAS_CRON_STORE', () => {
  const { root, agentsRoot, cronStore } = mkTmpDirs();
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  process.env.ATLAS_CRON_STORE = cronStore;
  try {
    seedDeployedAgent(agentsRoot, 'crbot', {
      build_id: 'B1', agent_name: 'crbot',
      triggers: [
        { req_id: 'R1', kind: 'cron', schedule: '0 8 * * 1', tz: 'America/New_York', pipeline: 'weekly.lobster' }
      ]
    }, { 'weekly.lobster': 'name: weekly\n' });

    const { register } = require('../atlas-trigger-registrar.js');
    const result = register({ buildId: 'B1', agentName: 'crbot' });
    assert.ok(result.success, JSON.stringify(result));
    assert.equal(result.registered.length, 1);
    assert.equal(result.registered[0].registered_as, 'B1-R1-1');
    assert.equal(result.registered[0].kind, 'cron');

    const store = JSON.parse(fs.readFileSync(cronStore, 'utf8'));
    assert.equal(store.version, 1);
    assert.equal(store.jobs.length, 1);
    const job = store.jobs[0];
    assert.equal(job.name, 'B1-R1-1');
    assert.equal(job.agentId, 'crbot');
    assert.equal(job.schedule.expr, '0 8 * * 1');
    assert.equal(job.schedule.tz, 'America/New_York');
    assert.equal(job.schedule.kind, 'cron');
    assert.equal(job.payload.kind, 'lobster');
    assert.equal(job.enabled, true);
    assert.ok(typeof job.id === 'string' && job.id.length > 0);
    assert.ok(typeof job.createdAtMs === 'number');
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
    delete process.env.ISOL8_AGENTS_DIR;
    delete process.env.ATLAS_CRON_STORE;
  }
});

test('register appends to existing jobs.json without clobbering', () => {
  const { root, agentsRoot, cronStore } = mkTmpDirs();
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  process.env.ATLAS_CRON_STORE = cronStore;
  try {
    fs.writeFileSync(cronStore, JSON.stringify({ version: 1, jobs: [{ id: 'pre', name: 'pre-existing' }] }));

    seedDeployedAgent(agentsRoot, 'crbot', {
      build_id: 'B2', agent_name: 'crbot',
      triggers: [{ req_id: 'R1', kind: 'cron', schedule: '* * * * *', pipeline: 'p.lobster' }]
    }, { 'p.lobster': 'name: p\n' });

    const { register } = require('../atlas-trigger-registrar.js');
    const result = register({ buildId: 'B2', agentName: 'crbot' });
    assert.ok(result.success);

    const store = JSON.parse(fs.readFileSync(cronStore, 'utf8'));
    assert.equal(store.jobs.length, 2);
    assert.equal(store.jobs[0].id, 'pre');  // pre-existing preserved
    assert.equal(store.jobs[1].name, 'B2-R1-1');
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
    delete process.env.ISOL8_AGENTS_DIR;
    delete process.env.ATLAS_CRON_STORE;
  }
});

test('register updates triggers.json with registered_as', () => {
  const { root, agentsRoot, cronStore } = mkTmpDirs();
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  process.env.ATLAS_CRON_STORE = cronStore;
  try {
    const dest = seedDeployedAgent(agentsRoot, 'crbot', {
      build_id: 'B3', agent_name: 'crbot',
      triggers: [{ req_id: 'R1', kind: 'cron', schedule: '* * * * *', pipeline: 'p.lobster' }]
    }, { 'p.lobster': 'name: p\n' });
    fs.writeFileSync(
      path.join(dest, 'workspace', 'triggers.json'),
      JSON.stringify({ agent_name: 'crbot', build_id: 'B3', triggers: [
        { req_id: 'R1', kind: 'cron', schedule: '* * * * *', pipeline: 'p.lobster', registered_as: null }
      ]})
    );

    const { register } = require('../atlas-trigger-registrar.js');
    const result = register({ buildId: 'B3', agentName: 'crbot' });
    assert.ok(result.success);

    const tj = JSON.parse(fs.readFileSync(path.join(dest, 'workspace', 'triggers.json'), 'utf8'));
    assert.equal(tj.triggers[0].registered_as, 'B3-R1-1');
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
    delete process.env.ISOL8_AGENTS_DIR;
    delete process.env.ATLAS_CRON_STORE;
  }
});
