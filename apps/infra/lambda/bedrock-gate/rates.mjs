// The FOURTH copy of the Bedrock rate card (the meter Lambda's index.py _RATES
// is the third; backend bedrock_pricing._RATES and the config.py catalog are
// the other two). This Lambda has no backend import path, so it cannot share
// the module; the lockstep guard is
// apps/backend/tests/unit/billing/test_bedrock_gate_rates_lockstep.py, which
// parses THIS file and compares it to the backend's tables (plus pricing-
// arithmetic parity). It runs on every backend PR (not vitest — infra-ci.yml
// does not run on backend PRs, so a vitest assertion would fire only after
// drift had already merged and deployed). Without it these WILL drift and the
// gate will reserve/settle the wrong dollar amount.
//
// USD per MILLION tokens, AWS `us.` Regional (CRIS) rate card. cache_read =
// 0.1x input; cache_write = the 1-HOUR tier (2x input) because
// agents.defaults.params.cacheRetention is pinned "long".

export const RATES = {
  "anthropic.claude-sonnet-4-6": { input: 3.3, output: 16.5, cache_write: 6.6, cache_read: 0.33 },
  "anthropic.claude-opus-4-6-v1": { input: 5.5, output: 27.5, cache_write: 11.0, cache_read: 0.55 },
  "anthropic.claude-haiku-4-5-20251001-v1:0": { input: 1.1, output: 5.5, cache_write: 2.2, cache_read: 0.11 },
};

// tier (the AIP "lane") -> rate-table key. Mirrors the meter's TIER_TO_FM and
// backend bedrock_aip.TIER_TO_FM (all lockstep-tested).
export const TIER_TO_FM = {
  opus: "anthropic.claude-opus-4-6-v1",
  sonnet: "anthropic.claude-sonnet-4-6",
  haiku: "anthropic.claude-haiku-4-5-20251001-v1:0",
};

export class UnknownTierError extends Error {}

// Model-max output ceilings for the maxTokens clamp (spec §5.1). NOT a rate —
// the abuse ceiling above which a present maxTokens is clamped. Adaptive
// traffic sends none, so this only ever bites hand-crafted/tampered requests.
export const MODEL_MAX_TOKENS = {
  opus: 131072,
  sonnet: 131072,
  haiku: 65536,
};

/**
 * Price one invocation in microcents from tier + usage counts.
 * Byte-for-byte the meter's _price_from_tier: per-component Math.trunc
 * (truncate each component, then sum — NOT truncate the sum). Because the rate
 * is USD/million and $1 = 1,000,000 microcents, tokens*rate IS microcents.
 * @throws {UnknownTierError}
 */
export function priceMicrocents(tier, { inputTokens = 0, outputTokens = 0, cacheReadTokens = 0, cacheWriteTokens = 0 }) {
  const fm = tier ? TIER_TO_FM[tier] : null;
  const rate = fm ? RATES[fm] : null;
  if (!rate) throw new UnknownTierError(String(tier));
  return (
    Math.trunc(inputTokens * rate.input) +
    Math.trunc(outputTokens * rate.output) +
    Math.trunc(cacheReadTokens * rate.cache_read) +
    Math.trunc(cacheWriteTokens * rate.cache_write)
  );
}
