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

module.exports = { validateTrigger, validateRequirementRow, VALID_KINDS };
