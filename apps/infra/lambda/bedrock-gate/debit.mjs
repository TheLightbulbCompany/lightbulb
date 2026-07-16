// Settle / release — the post-stream side of the ledger. Prices the actual
// usage the tee extracted and writes it (releasing the hold), or just releases
// the hold when there's nothing to charge (abort / upstream error).
import { priceMicrocents } from "./rates.mjs";
import { trialSettle, trialRelease, paidSettle, paidRelease } from "./ledger.mjs";

/**
 * Normalize the tee's usage into the four counts pricing needs.
 * Converse: metadata.usage.{inputTokens,outputTokens,cacheReadInputTokens,cacheWriteInputTokens}.
 * Invoke: amazon-bedrock-invocationMetrics.{inputTokenCount,outputTokenCount,
 *   cacheReadInputTokenCount,cacheWriteInputTokenCount} (cache fields present on newer Bedrock).
 */
export function normalizeUsage(usage) {
  if (!usage) return null;
  const pick = (...keys) => {
    for (const k of keys) if (typeof usage[k] === "number") return usage[k];
    return 0;
  };
  return {
    inputTokens: pick("inputTokens", "inputTokenCount", "input_tokens"),
    outputTokens: pick("outputTokens", "outputTokenCount", "output_tokens"),
    cacheReadTokens: pick("cacheReadInputTokens", "cacheReadInputTokenCount", "cache_read_input_tokens"),
    cacheWriteTokens: pick("cacheWriteInputTokens", "cacheWriteInputTokenCount", "cache_creation_input_tokens"),
  };
}

/**
 * Settle a completed request. Prices usage → actual microcents, then:
 *   trial → dedup-claim + ADD spent + release hold (or release-only on dup).
 *   paid  → decrement remaining estimate + release hold.
 * @returns {"counted"|"dup"|"released_no_usage"}
 */
export async function settle({ ddb, env, hold, tier, requestId, usage }) {
  const norm = normalizeUsage(usage);
  if (!norm || !requestId) {
    // No usage event (aborted before metadata) — free the hold; the meter
    // auditor will count the actual from the invocation log within ~90s.
    await release({ ddb, env, hold });
    return "released_no_usage";
  }
  const actual = priceMicrocents(tier, norm);
  const ttl = Number(env.DEDUP_TTL_SECONDS || 15 * 24 * 3600);
  if (hold.plan === "trial") {
    return await trialSettle(ddb, env, hold, requestId, actual, ttl);
  }
  await paidSettle(ddb, env, hold, actual);
  return "counted";
}

/** Release a hold without charging (finally-path; must never throw upward). */
export async function release({ ddb, env, hold }) {
  try {
    if (hold.plan === "trial") await trialRelease(ddb, env, hold);
    else await paidRelease(ddb, env, hold);
  } catch (e) {
    // A leaked hold self-heals: trial reserved maps reset at UTC rollover; paid
    // reserved is overwritten by the next Metronome refresh. Log via caller.
    throw e;
  }
}
