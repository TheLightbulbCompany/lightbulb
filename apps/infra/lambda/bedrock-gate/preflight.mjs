// Plan resolution, the maxTokens ceiling-clamp, and the pre-flight reservation
// orchestration. Budget enforcement itself lives in ledger.mjs.
import { readS, readN } from "./ddb.mjs";
import { trialReserve, paidReserve } from "./ledger.mjs";
import { trialCap, trialExpired, creditExhausted, ledgerUnprovisioned } from "./errors.mjs";

export const resolvePlan = (billingRow) =>
  readS(billingRow?.subscription_status) === "trial_local" ? "trial" : "paid";

export function isTrialExpired(billingRow, nowSec) {
  const end = readN(billingRow?.trial_end);
  return end !== undefined && nowSec > end;
}

/**
 * Ceiling-only clamp (spec §5.1). Adaptive traffic sends NO maxTokens → Bedrock
 * uses the model default (= model max); injecting a small cap would truncate
 * thinking. So: absent → leave absent; present ≤ modelMax → pass through;
 * present > modelMax → clamp to modelMax (config-tampering; Bedrock would reject
 * it anyway). Non-adaptive guard: never leave maxTokens ≤ thinking.budget_tokens
 * (Bedrock rejects budget ≥ max) — if the clamp would violate that, drop the key.
 * Mutates and returns `body`. `op` selects the wire key.
 */
export function clampMaxTokens(body, op, modelMax) {
  if (!body || typeof body !== "object") return body;
  const isInvoke = op === "invoke" || op === "invoke-with-response-stream";
  if (isInvoke) {
    if (typeof body.max_tokens === "number" && body.max_tokens > modelMax) body.max_tokens = modelMax;
    const budget = body?.thinking?.budget_tokens;
    if (typeof budget === "number" && typeof body.max_tokens === "number" && body.max_tokens <= budget) {
      delete body.max_tokens; // let Bedrock default rather than send an invalid pair
    }
    return body;
  }
  // Converse / ConverseStream
  const ic = body.inferenceConfig;
  if (ic && typeof ic.maxTokens === "number" && ic.maxTokens > modelMax) ic.maxTokens = modelMax;
  const budget = body?.additionalModelRequestFields?.thinking?.budget_tokens;
  if (typeof budget === "number" && ic && typeof ic.maxTokens === "number" && ic.maxTokens <= budget) {
    delete ic.maxTokens;
  }
  return body;
}

/**
 * Pre-flight admission with a reservation hold.
 * @returns {{hold: object}}  on admit
 * @throws {GateError} 429 on any budget denial
 */
export async function reserveOrThrow({ ddb, env, ownerId, plan, billingRow, fastGateRow, today, nowSec }) {
  const estimate = Number(env.RESERVE_ESTIMATE_MICROCENTS);
  if (plan === "trial") {
    if (isTrialExpired(billingRow, nowSec)) throw trialExpired();
    const cap = Number(env.CAP_MICROCENTS);
    const r = await trialReserve(ddb, env, ownerId, today, estimate, cap, billingRow);
    if (!r.ok) throw trialCap();
    return { hold: { ...r.hold, plan: "trial" } };
  }
  const r = await paidReserve(ddb, env, ownerId, estimate, fastGateRow, billingRow);
  // "No ledger provisioned" and "balance drained" are both 429s to the caller
  // but opposite operational conditions — the first is our bug, the second is
  // normal business. Distinct codes so the metric distinguishes them.
  if (!r.ok) throw r.unprovisioned ? ledgerUnprovisioned() : creditExhausted();
  return { hold: { ...r.hold, plan: "paid" } };
}
