// writeDisk must recursively walk staging-dir subdirectories.
// Today's implementation reads only files at the staging root and drops
// any nested directories — workspace/ would be silently lost.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');

function makeTmpAgentsRoot() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'atlas-test-agents-'));
}

function seedStaging(agentsRoot, buildId, files) {
  const staging = path.join(agentsRoot, `.atlas-staging-${buildId}`);
  fs.mkdirSync(staging);
  for (const [relPath, content] of Object.entries(files)) {
    const full = path.join(staging, relPath);
    fs.mkdirSync(path.dirname(full), { recursive: true });
    fs.writeFileSync(full, content);
  }
  return staging;
}

test('writeDisk preserves nested workspace/ subdirectory', () => {
  const agentsRoot = makeTmpAgentsRoot();
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  const buildId = 'build_recursive_1';
  seedStaging(agentsRoot, buildId, {
    'build-manifest.json': JSON.stringify({ build_id: buildId, agent_name: 'rebot' }),
    'workspace/openclaw.json': JSON.stringify({ agents: { list: [{ id: 'rebot' }] } }),
    'workspace/AGENTS.md': '# AGENTS\n',
    'workspace/skills/foo/SKILL.md': '# foo\n',
    'workspace/p.lobster': 'name: p\n'
  });
  const { writeDisk } = require('../atlas-skeleton-generator.js');
  const result = writeDisk(buildId, 'rebot');
  assert.ok(result.success, JSON.stringify(result));
  // Nested files preserved.
  assert.ok(fs.existsSync(path.join(agentsRoot, 'rebot', 'workspace', 'openclaw.json')));
  assert.ok(fs.existsSync(path.join(agentsRoot, 'rebot', 'workspace', 'AGENTS.md')));
  assert.ok(fs.existsSync(path.join(agentsRoot, 'rebot', 'workspace', 'skills', 'foo', 'SKILL.md')));
  assert.ok(fs.existsSync(path.join(agentsRoot, 'rebot', 'workspace', 'p.lobster')));
  // build-manifest.json at root.
  assert.ok(fs.existsSync(path.join(agentsRoot, 'rebot', 'build-manifest.json')));
  // file_hashes contains every nested file (relative paths).
  assert.ok(result.file_hashes['workspace/skills/foo/SKILL.md']);
  assert.equal(result.file_count, 5);
});

test('verifyWrite recursively hashes nested files', () => {
  const agentsRoot = makeTmpAgentsRoot();
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  const destDir = path.join(agentsRoot, 'vbot');
  fs.mkdirSync(path.join(destDir, 'workspace', 'inner'), { recursive: true });
  fs.writeFileSync(path.join(destDir, 'build-manifest.json'), 'm');
  fs.writeFileSync(path.join(destDir, 'workspace', 'a.md'), 'a');
  fs.writeFileSync(path.join(destDir, 'workspace', 'inner', 'b.md'), 'b');

  const { verifyWrite } = require('../atlas-skeleton-generator.js');
  const result = verifyWrite('build_v_1', 'vbot');
  assert.ok(result.success);
  assert.equal(result.file_count, 3);
  assert.ok(result.observed_hashes['workspace/a.md']);
  assert.ok(result.observed_hashes['workspace/inner/b.md']);
  assert.ok(result.observed_hashes['build-manifest.json']);
});
