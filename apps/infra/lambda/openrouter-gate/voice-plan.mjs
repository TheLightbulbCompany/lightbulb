// Mirrors core.billing.voice_billing.has_voice_plan over DynamoDB AttributeValues.
// Collected coverage, debt and confirmed cancellation are checked on every inference request.
export function hasVoicePlan(row, now = Date.now()) {
  if (!row?.metronome_voice_contract_id?.S || row?.voice_plan_status?.S === 'canceled') return false;
  const debts = row.voice_plan_unpaid_invoice_ids;
  if (debts && (!Array.isArray(debts.SS) || debts.SS.length)) return false;
  const ending = row.voice_plan_ending_at?.S;
  if (ending && !(Date.parse(ending) > now)) return false;
  return Object.values(row.voice_plan_paid_periods?.M ?? {}).some(value => {
    const period = value?.M;
    const start = Date.parse(period?.start?.S);
    const end = Date.parse(period?.end?.S);
    return Number.isFinite(start) && Number.isFinite(end) && start <= now && now < end;
  });
}
