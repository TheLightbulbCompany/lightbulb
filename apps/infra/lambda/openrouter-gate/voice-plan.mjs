// Mirrors core.billing.voice_billing.has_voice_plan over DynamoDB AttributeValues.
// The $250 flat fee is a Stripe subscription: active/trialing (a 100%-off code is active) is the entitlement.
export function hasVoicePlan(row, now = Date.now()) {
  if (!row?.metronome_voice_contract_id?.S || row?.voice_plan_status?.S === 'canceled') return false;
  if (!['active', 'trialing'].includes(row?.voice_plan_stripe_status?.S)) return false;
  const ending = row.voice_plan_ending_at?.S;
  return !ending || Date.parse(ending) > now;
}
