// End-to-end synthetic build smoke test. Exercises:
//   draftToFastIo → stages workspace/ tree + manifest
//   writeDisk      → atomic mv into agentsRoot/{name}/
//   register       → writes cron row into ATLAS_CRON_STORE
//   deregister     → removes only this build's rows

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');

function makeRoots() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'atlas-e2e-'));
  return {
    root,
    agentsRoot: path.join(root, 'agents'),
    cronStore: path.join(root, 'cron', 'jobs.json')
  };
}

function specFor(buildId) {
  return {
    build_id: buildId,
    intake: { name: 'e2ebot', archetype: 'general', build_id: buildId },
    user_stories: [
      { req_id: 'R1', agent_name: 'e2ebot', action: 'send weekly digest', outcome: 'team sees status weekly' },
      { req_id: 'R2', agent_name: 'e2ebot', action: 'respond to transcript', outcome: 'meeting notes captured' },
      { req_id: 'R3', agent_name: 'e2ebot', action: 'answer questions', outcome: 'user gets answers' }
    ],
    never_does: ['never spam channels'],
    pipelines: [
      { name: 'weekly-digest', purpose: 'cron-driven weekly summary' },
      { name: 'meeting-process', purpose: 'hook-driven transcript handler' }
    ],
    requirements_with_triggers: [
      { req_id: 'R1', classification: 'deterministic',
        triggers: [{ kind: 'cron', schedule: '0 8 * * 1', tz: 'America/New_York', pipeline: 'weekly-digest.lobster' }] },
      { req_id: 'R2', classification: 'deterministic',
        triggers: [{ kind: 'hook', route: '/hooks/transcript-ready', pipeline: 'meeting-process.lobster' }] },
      { req_id: 'R3', classification: 'llm', triggers: [] }
    ]
  };
}

function stageThenWrite(spec, agentsRoot) {
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  fs.mkdirSync(agentsRoot, { recursive: true });
  const gen = require('../atlas-skeleton-generator.js');
  const drafted = gen.draftToFastIo(spec);
  assert.ok(drafted.pass, JSON.stringify(drafted.errors));

  // Materialize to the staging dir as build.lobster step 6.7 would.
  const stagingDir = path.join(agentsRoot, `.atlas-staging-${spec.build_id}`);
  fs.mkdirSync(stagingDir, { recursive: true });
  for (const [rel, content] of Object.entries(drafted.artifacts_to_stage)) {
    const full = path.join(stagingDir, rel);
    fs.mkdirSync(path.dirname(full), { recursive: true });
    fs.writeFileSync(full, content);
  }
  return gen.writeDisk(spec.build_id, spec.intake.name);
}

test('end-to-end: stage → writeDisk → register places workspace tree + cron row', () => {
  const { root, agentsRoot, cronStore } = makeRoots();
  process.env.ATLAS_CRON_STORE = cronStore;
  try {
    const spec = specFor('build_e2e_happy');
    const written = stageThenWrite(spec, agentsRoot);
    assert.ok(written.success, JSON.stringify(written));

    const dest = path.join(agentsRoot, 'e2ebot');
    for (const f of ['AGENTS.md', 'SOUL.md', 'openclaw.json', 'triggers.json',
                     'weekly-digest.lobster', 'meeting-process.lobster']) {
      assert.ok(fs.existsSync(path.join(dest, 'workspace', f)), `missing ${f}`);
    }
    assert.ok(fs.existsSync(path.join(dest, 'build-manifest.json')));

    const reg = require('../atlas-trigger-registrar.js');
    const regResult = reg.register({ buildId: spec.build_id, agentName: spec.intake.name });
    assert.ok(regResult.success, JSON.stringify(regResult));
    assert.equal(regResult.cron_registered_count, 1);
    assert.equal(regResult.hook_asserted_count, 1);

    const store = JSON.parse(fs.readFileSync(cronStore, 'utf8'));
    assert.equal(store.jobs.length, 1);
    assert.equal(store.jobs[0].name, 'build_e2e_happy-R1-1');
    assert.equal(store.jobs[0].agentId, 'e2ebot');

    const tj = JSON.parse(fs.readFileSync(path.join(dest, 'workspace', 'triggers.json'), 'utf8'));
    const cronEntry = tj.triggers.find(t => t.kind === 'cron');
    assert.equal(cronEntry.registered_as, 'build_e2e_happy-R1-1');
    const hookEntry = tj.triggers.find(t => t.kind === 'hook');
    assert.equal(hookEntry.registered_as, 'framework-routed:/hooks/transcript-ready');
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
    delete process.env.ISOL8_AGENTS_DIR;
    delete process.env.ATLAS_CRON_STORE;
  }
});

test('end-to-end: rollback cleans cron rows for the failing build', () => {
  const { root, agentsRoot, cronStore } = makeRoots();
  process.env.ATLAS_CRON_STORE = cronStore;
  try {
    const spec = specFor('build_e2e_rollback');
    const written = stageThenWrite(spec, agentsRoot);
    assert.ok(written.success);

    const reg = require('../atlas-trigger-registrar.js');
    reg.register({ buildId: spec.build_id, agentName: spec.intake.name });

    let store = JSON.parse(fs.readFileSync(cronStore, 'utf8'));
    assert.equal(store.jobs.length, 1);

    const dereg = reg.deregister({ buildId: spec.build_id, agentName: spec.intake.name });
    assert.ok(dereg.success);
    assert.equal(dereg.cron_removed, 1);

    store = JSON.parse(fs.readFileSync(cronStore, 'utf8'));
    assert.equal(store.jobs.length, 0);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
    delete process.env.ISOL8_AGENTS_DIR;
    delete process.env.ATLAS_CRON_STORE;
  }
});

test('end-to-end: determinism-audit refuses build when deterministic req has no triggers', () => {
  const { root, agentsRoot } = makeRoots();
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  try {
    const spec = specFor('build_e2e_audit_fail');
    // Force R1 to deterministic with NO triggers.
    spec.requirements_with_triggers[0].triggers = [];
    stageThenWrite(spec, agentsRoot);

    // Note: stageThenWrite already mv'd the staging dir to agentsRoot/e2ebot/.
    // For the audit to find pipeline files in a STAGING dir, recreate a
    // dedicated staging-only dir with the pipelines.
    const stagingDir = path.join(agentsRoot, `.atlas-staging-${spec.build_id}-audit`);
    fs.mkdirSync(path.join(stagingDir, 'workspace'), { recursive: true });
    // Provide pipeline files for R2's hook trigger so only R1 (no triggers) fails.
    fs.writeFileSync(path.join(stagingDir, 'workspace', 'weekly-digest.lobster'), 'name: x\n');
    fs.writeFileSync(path.join(stagingDir, 'workspace', 'meeting-process.lobster'), 'name: y\n');

    const { audit } = require('../atlas-determinism-audit.js');
    const result = audit({
      buildId: spec.build_id,
      stagingDir,
      _ioAdapter: {
        readMatrix: () => ({ user_stories: spec.requirements_with_triggers.map(r => ({ ...r })) }),
        listDecisions: () => spec.requirements_with_triggers.map(r => ({ req_id: r.req_id, decision: 'approve' }))
      }
    });
    assert.ok(!result.pass);
    assert.equal(result.failures.length, 1);
    assert.equal(result.failures[0].req_id, 'R1');
    assert.match(result.failures[0].reason, /no_triggers/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
    delete process.env.ISOL8_AGENTS_DIR;
    delete process.env.ATLAS_CRON_STORE;
  }
});
