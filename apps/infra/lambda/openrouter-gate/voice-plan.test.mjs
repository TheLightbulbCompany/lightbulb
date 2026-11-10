import test from 'node:test';
import assert from 'node:assert/strict';
import { hasVoicePlan } from './voice-plan.mjs';

const now = Date.parse('2026-11-08T12:00:00Z');
const row = { metronome_voice_contract_id: { S: 'voice' }, voice_plan_stripe_status: { S: 'active' } };

test('an active or trialing Stripe subscription (100%-off included) is the entitlement', () => {
  assert.equal(hasVoicePlan(row, now), true);
  assert.equal(hasVoicePlan({ ...row, voice_plan_stripe_status: { S: 'trialing' } }, now), true);
  for (const s of ['past_due', 'incomplete', 'canceled', 'unpaid']) {
    assert.equal(hasVoicePlan({ ...row, voice_plan_stripe_status: { S: s } }, now), false);
  }
  assert.equal(hasVoicePlan({ metronome_voice_contract_id: { S: 'voice' } }, now), false);
});
test('no usage contract, a confirmed cancellation or a passed end date deny', () => {
  assert.equal(hasVoicePlan({ voice_plan_stripe_status: { S: 'active' } }, now), false);
  assert.equal(hasVoicePlan({ ...row, voice_plan_status: { S: 'canceled' } }, now), false);
  assert.equal(hasVoicePlan({ ...row, voice_plan_ending_at: { S: '2026-11-08T00:00:00Z' } }, now), false);
  assert.equal(hasVoicePlan({ ...row, voice_plan_ending_at: { S: '2026-12-01T00:00:00Z' } }, now), true);
});
