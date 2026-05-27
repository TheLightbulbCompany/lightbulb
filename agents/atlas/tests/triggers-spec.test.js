// Pins the per-requirement triggers[] schema consumed by the skeleton
// generator and audited by atlas-determinism-audit.js.
// The schema is enforced in code (no JSON Schema file) — this test is the
// canonical reference for the shape.

const test = require('node:test');
const assert = require('node:assert/strict');

const VALID_KINDS = new Set(['cron', 'hook', 'agent-call']);

function validateTrigger(t) {
  if (!t || typeof t !== 'object') return { ok: false, error: 'trigger must be an object' };
  if (!VALID_KINDS.has(t.kind)) return { ok: false, error: `kind must be one of cron|hook|agent-call (got ${t.kind})` };
  if (t.kind === 'cron') {
    if (typeof t.schedule !== 'string' || t.schedule.trim() === '') return { ok: false, error: 'cron trigger requires non-empty schedule' };
    if (typeof t.pipeline !== 'string' || !t.pipeline.endsWith('.lobster')) return { ok: false, error: 'cron trigger requires .lobster pipeline' };
  } else if (t.kind === 'hook') {
    if (typeof t.route !== 'string' || !t.route.startsWith('/hooks/')) return { ok: false, error: 'hook trigger requires route starting with /hooks/' };
    if (typeof t.pipeline !== 'string' || !t.pipeline.endsWith('.lobster')) return { ok: false, error: 'hook trigger requires .lobster pipeline' };
  } else if (t.kind === 'agent-call') {
    if (typeof t.callable_as !== 'string') return { ok: false, error: 'agent-call trigger requires callable_as string' };
    if (t.pipeline !== null && (typeof t.pipeline !== 'string' || !t.pipeline.endsWith('.lobster'))) return { ok: false, error: 'agent-call pipeline must be null or .lobster string' };
  }
  return { ok: true };
}

function validateRequirementRow(row) {
  const triggers = row.triggers || [];
  if (row.classification === 'deterministic' && triggers.length === 0) {
    return { ok: false, error: `deterministic requirement ${row.req_id} has no triggers` };
  }
  for (const t of triggers) {
    const r = validateTrigger(t);
    if (!r.ok) return { ok: false, error: `${row.req_id}: ${r.error}` };
  }
  return { ok: true };
}

test('cron trigger requires schedule + .lobster pipeline', () => {
  assert.ok(validateTrigger({ kind: 'cron', schedule: '0 8 * * 1', pipeline: 'weekly.lobster' }).ok);
  assert.ok(!validateTrigger({ kind: 'cron', schedule: '0 8 * * 1', pipeline: 'weekly.js' }).ok);
  assert.ok(!validateTrigger({ kind: 'cron', pipeline: 'weekly.lobster' }).ok);
});

test('hook trigger requires /hooks/ route + .lobster pipeline', () => {
  assert.ok(validateTrigger({ kind: 'hook', route: '/hooks/foo', pipeline: 'x.lobster' }).ok);
  assert.ok(!validateTrigger({ kind: 'hook', route: '/foo', pipeline: 'x.lobster' }).ok);
});

test('agent-call may have null pipeline', () => {
  assert.ok(validateTrigger({ kind: 'agent-call', callable_as: 'agent.invoke(...)', pipeline: null }).ok);
  assert.ok(validateTrigger({ kind: 'agent-call', callable_as: 'agent.invoke(...)', pipeline: 'p.lobster' }).ok);
});

test('channel kind is rejected in v1', () => {
  assert.ok(!validateTrigger({ kind: 'channel', channel: 'slack', match: '@echo', pipeline: 'x.lobster' }).ok);
});

test('deterministic requirement with no triggers fails', () => {
  assert.ok(!validateRequirementRow({ req_id: 'R1', classification: 'deterministic', triggers: [] }).ok);
});

test('llm requirement may have zero triggers', () => {
  assert.ok(validateRequirementRow({ req_id: 'R2', classification: 'llm', triggers: [] }).ok);
});

test('draftToFastIo emits triggers.json under workspace/ root', () => {
  const { draftToFastIo } = require('../atlas-skeleton-generator.js');
  const result = draftToFastIo({
    intake: { name: 'testbot', archetype: 'general' },
    user_stories: [
      { req_id: 'R1', agent_name: 'testbot', action: 'do thing', outcome: 'thing is done' }
    ],
    never_does: ['never break things'],
    skills_required: [],
    pipelines: [{ name: 'do-thing', purpose: 'does thing' }],
    requirements_with_triggers: [
      { req_id: 'R1', classification: 'deterministic',
        triggers: [{ kind: 'cron', schedule: '0 8 * * 1', pipeline: 'do-thing.lobster' }] }
    ],
    build_id: 'build_test'
  });
  assert.ok(result.pass, JSON.stringify(result.errors));
  assert.ok(result.artifacts_to_stage['workspace/triggers.json'], 'triggers.json must be under workspace/');
  const parsed = JSON.parse(result.artifacts_to_stage['workspace/triggers.json']);
  assert.equal(parsed.agent_name, 'testbot');
  assert.equal(parsed.build_id, 'build_test');
  assert.equal(parsed.triggers.length, 1);
  assert.equal(parsed.triggers[0].req_id, 'R1');
  assert.equal(parsed.triggers[0].kind, 'cron');
});

test('draftToFastIo emits build-manifest.json with files[] and triggers[]', () => {
  const { draftToFastIo } = require('../atlas-skeleton-generator.js');
  const result = draftToFastIo({
    intake: { name: 'testbot' },
    user_stories: [{ req_id: 'R1', agent_name: 'testbot', action: 'x', outcome: 'y' }],
    never_does: ['nope'],
    requirements_with_triggers: [],
    build_id: 'build_test'
  });
  assert.ok(result.pass);
  assert.ok(result.artifacts_to_stage['build-manifest.json']);
  const m = JSON.parse(result.artifacts_to_stage['build-manifest.json']);
  assert.equal(m.build_id, 'build_test');
  assert.equal(m.agent_name, 'testbot');
  assert.ok(Array.isArray(m.files));
  assert.ok(Array.isArray(m.triggers));
});

test('draftToFastIo places ALL generated agent files under workspace/', () => {
  const { draftToFastIo } = require('../atlas-skeleton-generator.js');
  const result = draftToFastIo({
    intake: { name: 'testbot' },
    user_stories: [{ req_id: 'R1', agent_name: 'testbot', action: 'x', outcome: 'y' }],
    never_does: ['nope'],
    pipelines: [{ name: 'p1', purpose: 'pp' }],
    scripts: [{ name: 's1', purpose: 'ss' }],
    requirements_with_triggers: [],
    build_id: 'build_test'
  });
  assert.ok(result.pass);
  for (const name of ['AGENTS.md', 'SOUL.md', 'IDENTITY.md', 'TOOLS.md', 'USER.md',
                      'BOOTSTRAP.md', 'openclaw.json', 'p1.lobster', 's1.js']) {
    assert.ok(result.artifacts_to_stage[`workspace/${name}`],
      `${name} must be under workspace/ (got keys: ${Object.keys(result.artifacts_to_stage).join(',')})`);
  }
  // build-manifest.json is the ONLY artifact at the staging root.
  assert.ok(result.artifacts_to_stage['build-manifest.json']);
});

module.exports = { validateTrigger, validateRequirementRow, VALID_KINDS };
