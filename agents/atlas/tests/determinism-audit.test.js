const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');

function mkStaging(buildId, files = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'atlas-audit-'));
  const agentsRoot = path.join(root, 'agents');
  const staging = path.join(agentsRoot, `.atlas-staging-${buildId}`);
  fs.mkdirSync(path.join(staging, 'workspace'), { recursive: true });
  for (const [rel, content] of Object.entries(files)) {
    const full = path.join(staging, 'workspace', rel);
    fs.mkdirSync(path.dirname(full), { recursive: true });
    fs.writeFileSync(full, content);
  }
  return { root, agentsRoot, stagingDir: staging };
}

function mockIo({ matrix, decisions }) {
  return {
    readMatrix: () => matrix,
    listDecisions: () => decisions
  };
}

test('audit passes when every deterministic req has triggers + pipelines exist', () => {
  const { root, stagingDir, agentsRoot } = mkStaging('B1', { 'p.lobster': 'name: p\n' });
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  try {
    const matrix = { user_stories: [
      { req_id: 'R1', classification: 'deterministic',
        triggers: [{ kind: 'cron', schedule: '* * * * *', pipeline: 'p.lobster' }] },
      { req_id: 'R2', classification: 'llm', triggers: [] }
    ]};
    const decisions = [
      { req_id: 'R1', decision: 'approve' },
      { req_id: 'R2', decision: 'approve' }
    ];
    const { audit } = require('../atlas-determinism-audit.js');
    const result = audit({ buildId: 'B1', stagingDir, _ioAdapter: mockIo({ matrix, decisions }) });
    assert.ok(result.pass, JSON.stringify(result));
    assert.equal(result.failures.length, 0);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
    delete process.env.ISOL8_AGENTS_DIR;
  }
});

test('audit fails when deterministic req has zero triggers', () => {
  const { root, stagingDir, agentsRoot } = mkStaging('B2');
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  try {
    const matrix = { user_stories: [
      { req_id: 'R1', classification: 'deterministic', triggers: [] }
    ]};
    const decisions = [{ req_id: 'R1', decision: 'approve' }];
    const { audit } = require('../atlas-determinism-audit.js');
    const result = audit({ buildId: 'B2', stagingDir, _ioAdapter: mockIo({ matrix, decisions }) });
    assert.ok(!result.pass);
    assert.equal(result.failures.length, 1);
    assert.equal(result.failures[0].req_id, 'R1');
    assert.match(result.failures[0].reason, /no_triggers/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
    delete process.env.ISOL8_AGENTS_DIR;
  }
});

test('audit fails when trigger pipeline missing from staging', () => {
  const { root, stagingDir, agentsRoot } = mkStaging('B3');
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  try {
    const matrix = { user_stories: [
      { req_id: 'R1', classification: 'deterministic',
        triggers: [{ kind: 'cron', schedule: '* * * * *', pipeline: 'p.lobster' }] }
    ]};
    const decisions = [{ req_id: 'R1', decision: 'approve' }];
    const { audit } = require('../atlas-determinism-audit.js');
    const result = audit({ buildId: 'B3', stagingDir, _ioAdapter: mockIo({ matrix, decisions }) });
    assert.ok(!result.pass);
    assert.equal(result.failures.length, 1);
    assert.match(result.failures[0].reason, /pipeline_missing/);
    assert.equal(result.failures[0].pipeline, 'p.lobster');
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
    delete process.env.ISOL8_AGENTS_DIR;
  }
});

test('audit ignores dropped requirements', () => {
  const { root, stagingDir, agentsRoot } = mkStaging('B4');
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  try {
    const matrix = { user_stories: [
      { req_id: 'R1', classification: 'deterministic', triggers: [] }
    ]};
    const decisions = [{ req_id: 'R1', decision: 'drop', user_note: 'not needed' }];
    const { audit } = require('../atlas-determinism-audit.js');
    const result = audit({ buildId: 'B4', stagingDir, _ioAdapter: mockIo({ matrix, decisions }) });
    assert.ok(result.pass, 'dropped reqs must be skipped: ' + JSON.stringify(result));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
    delete process.env.ISOL8_AGENTS_DIR;
  }
});

test('audit honors classification overrides in the decision', () => {
  const { root, stagingDir, agentsRoot } = mkStaging('B5');
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  try {
    const matrix = { user_stories: [
      { req_id: 'R1', classification: 'deterministic', triggers: [] }
    ]};
    const decisions = [{ req_id: 'R1', decision: 'change', classification_override: 'llm' }];
    const { audit } = require('../atlas-determinism-audit.js');
    const result = audit({ buildId: 'B5', stagingDir, _ioAdapter: mockIo({ matrix, decisions }) });
    assert.ok(result.pass);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
    delete process.env.ISOL8_AGENTS_DIR;
  }
});

test('audit fails with multiple failures aggregated', () => {
  const { root, stagingDir, agentsRoot } = mkStaging('B6');
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  try {
    const matrix = { user_stories: [
      { req_id: 'R1', classification: 'deterministic', triggers: [] },
      { req_id: 'R2', classification: 'deterministic',
        triggers: [{ kind: 'cron', schedule: '* * * * *', pipeline: 'gone.lobster' }] }
    ]};
    const decisions = [
      { req_id: 'R1', decision: 'approve' },
      { req_id: 'R2', decision: 'approve' }
    ];
    const { audit } = require('../atlas-determinism-audit.js');
    const result = audit({ buildId: 'B6', stagingDir, _ioAdapter: mockIo({ matrix, decisions }) });
    assert.ok(!result.pass);
    assert.equal(result.failures.length, 2);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
    delete process.env.ISOL8_AGENTS_DIR;
  }
});
