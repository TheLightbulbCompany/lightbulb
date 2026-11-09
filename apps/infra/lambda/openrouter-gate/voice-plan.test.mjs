import test from 'node:test';
import assert from 'node:assert/strict';
import { hasVoicePlan } from './voice-plan.mjs';

const period = (start, end) => ({ M: { start: { S: start }, end: { S: end } } });
const now = Date.parse('2026-11-08T12:00:00Z');
const current = period('2026-11-01T00:00:00Z', '2026-12-01T00:00:00Z');
const row = { metronome_voice_contract_id: { S: 'voice' }, voice_plan_status: { S: 'pending_payment' }, voice_plan_paid_periods: { M: { current } } };

test('collected current voice coverage grants a bounded inference entitlement', () => {
  assert.equal(hasVoicePlan(row, now), true);
  assert.equal(hasVoicePlan(row, Date.parse('2026-12-01T00:00:00Z')), false);
  assert.equal(hasVoicePlan({ ...row, voice_plan_paid_periods: { M: {} } }, now), false);
});
test('prepaid future period cannot unlock now or hide a separately paid current period', () => {
  const future = period('2027-01-01T00:00:00Z', '2027-02-01T00:00:00Z');
  assert.equal(hasVoicePlan({ ...row, voice_plan_paid_periods: { M: { future } } }, now), false);
  assert.equal(hasVoicePlan({ ...row, voice_plan_paid_periods: { M: { current, future } } }, now), true);
});
test('debt and confirmed cancellation deny; an unconfirmed cancellation request does not', () => {
  assert.equal(hasVoicePlan({ ...row, voice_plan_unpaid_invoice_ids: { SS: ['in_unpaid'] } }, now), false);
  assert.equal(hasVoicePlan({ ...row, voice_plan_ending_at: { S: '2026-11-08T00:00:00Z' } }, now), false);
  assert.equal(hasVoicePlan({ ...row, voice_plan_cancel_target: { S: '2026-11-08T00:00:00Z' } }, now), true);
});
