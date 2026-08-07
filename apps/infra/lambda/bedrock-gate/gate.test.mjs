import { test } from "node:test";
import assert from "node:assert/strict";
import { makeFakeDdb } from "./fake-ddb.mjs";
import { priceMicrocents, MODEL_MAX_TOKENS } from "./rates.mjs";
import { hashToken, authenticate, tierForModel } from "./auth.mjs";
import { resolvePlan, isTrialExpired, clampMaxTokens, reserveOrThrow } from "./preflight.mjs";
import { settle, release, normalizeUsage } from "./debit.mjs";
import { readMapN, readN } from "./ddb.mjs";

const ENV = {
  AWS_REGION: "us-east-1", GATE_ENV: "test",
  TOKENS_TABLE: "tok", BILLING_TABLE: "bill", FAST_GATE_TABLE: "fg", INVOCATION_DEDUP_TABLE: "dd",
  CAP_MICROCENTS: "3000000", RESERVE_ESTIMATE_MICROCENTS: "300000", DEDUP_TTL_SECONDS: "1296000",
};
const TODAY = "2026-07-15";
const AIP = "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/abc123";

// ---- pricing: byte-equal to the meter's per-component truncation ----------
test("priceMicrocents matches the meter's per-component truncation", () => {
  // 1M sonnet input = $3.30 = 3,300,000 microcents
  assert.equal(priceMicrocents("sonnet", { inputTokens: 1_000_000 }), 3_300_000);
  // per-component trunc, not sum-trunc
  const p = priceMicrocents("sonnet", { inputTokens: 1, outputTokens: 1, cacheReadTokens: 1, cacheWriteTokens: 1 });
  assert.equal(p, Math.trunc(3.3) + Math.trunc(16.5) + Math.trunc(0.33) + Math.trunc(6.6)); // 3+16+0+6 = 25
});
test("unknown tier throws", () => {
  assert.throws(() => priceMicrocents("mystery", { inputTokens: 1 }), /mystery/);
});

// ---- auth -----------------------------------------------------------------
function seedToken(store, token, { ownerId = "user_x", denied = false, tiers = { [AIP]: "sonnet" } } = {}) {
  store.tok ||= {};
  store.tok[hashToken(token)] = {
    token_hash: { S: hashToken(token) }, owner_id: { S: ownerId },
    denied: { BOOL: denied }, aip_tiers: { M: Object.fromEntries(Object.entries(tiers).map(([k, v]) => [k, { S: v }])) },
  };
}
test("authenticate: missing header → 401", async () => {
  const ddb = makeFakeDdb();
  await assert.rejects(authenticate({}, ddb, ENV), (e) => e.status === 401);
});
test("authenticate: unknown token → 401", async () => {
  const ddb = makeFakeDdb();
  await assert.rejects(authenticate({ authorization: "Bearer nope" }, ddb, ENV), (e) => e.status === 401);
});
test("authenticate: denied flag → 403", async () => {
  const ddb = makeFakeDdb();
  seedToken(ddb._store, "t1", { denied: true });
  await assert.rejects(authenticate({ authorization: "Bearer t1" }, ddb, ENV), (e) => e.status === 403 && e.code === "denied");
});
test("authenticate: valid → owner + tiers; tierForModel resolves & guards", async () => {
  const ddb = makeFakeDdb();
  seedToken(ddb._store, "t1");
  const { ownerId, aipTiers } = await authenticate({ authorization: "Bearer t1" }, ddb, ENV);
  assert.equal(ownerId, "user_x");
  assert.equal(tierForModel(AIP, aipTiers), "sonnet");
  assert.throws(() => tierForModel("arn:...:application-inference-profile/other", aipTiers), (e) => e.status === 403);
});

// ---- clamp: ceiling only, never a small cap -------------------------------
test("clamp: absent maxTokens stays absent (no truncation of adaptive turns)", () => {
  const body = { messages: [], additionalModelRequestFields: { thinking: { type: "adaptive" } } };
  clampMaxTokens(body, "converse-stream", MODEL_MAX_TOKENS.sonnet);
  assert.equal(body.inferenceConfig, undefined);
});
test("clamp: present ≤ model max passes through", () => {
  const body = { inferenceConfig: { maxTokens: 8192 } };
  clampMaxTokens(body, "converse-stream", MODEL_MAX_TOKENS.sonnet);
  assert.equal(body.inferenceConfig.maxTokens, 8192);
});
test("clamp: present > model max clamped to max (tamper ceiling)", () => {
  const body = { inferenceConfig: { maxTokens: 999999 } };
  clampMaxTokens(body, "converse-stream", MODEL_MAX_TOKENS.sonnet);
  assert.equal(body.inferenceConfig.maxTokens, MODEL_MAX_TOKENS.sonnet);
});
test("clamp: invoke body max_tokens > max clamped; budget<max invariant drops key", () => {
  const b1 = { max_tokens: 999999 };
  clampMaxTokens(b1, "invoke-with-response-stream", MODEL_MAX_TOKENS.haiku);
  assert.equal(b1.max_tokens, MODEL_MAX_TOKENS.haiku);
  const b2 = { max_tokens: 1000, thinking: { budget_tokens: 2000 } }; // max <= budget → invalid
  clampMaxTokens(b2, "invoke", MODEL_MAX_TOKENS.haiku);
  assert.equal(b2.max_tokens, undefined);
});

// ---- plan + expiry --------------------------------------------------------
test("resolvePlan trial vs paid", () => {
  assert.equal(resolvePlan({ subscription_status: { S: "trial_local" } }), "trial");
  assert.equal(resolvePlan({ subscription_status: { S: "active" } }), "paid");
  assert.equal(resolvePlan(undefined), "paid");
});
test("isTrialExpired", () => {
  assert.equal(isTrialExpired({ trial_end: { N: "100" } }, 200), true);
  assert.equal(isTrialExpired({ trial_end: { N: "300" } }, 200), false);
  assert.equal(isTrialExpired({}, 200), false);
});

// ---- trial reservation ledger --------------------------------------------
function trialBilling(store, ownerId, { spent = {}, reserved = {}, status = "trial_local", trialEnd } = {}) {
  store.bill ||= {};
  const toMap = (o) => ({ M: Object.fromEntries(Object.entries(o).map(([k, v]) => [k, { N: String(v) }])) });
  store.bill[ownerId] = {
    owner_id: { S: ownerId }, subscription_status: { S: status },
    trial_daily_spend_microcents: toMap(spent), reserved_microcents: toMap(reserved),
    ...(trialEnd ? { trial_end: { N: String(trialEnd) } } : {}),
  };
}
const billingRow = (ddb, o) => ddb._store.bill[o];

test("trial reserve: admits under cap and records the hold", async () => {
  const ddb = makeFakeDdb();
  trialBilling(ddb._store, "u", { spent: { [TODAY]: 1_000_000 } });
  const { hold } = await reserveOrThrow({ ddb, env: ENV, ownerId: "u", plan: "trial", billingRow: billingRow(ddb, "u"), today: TODAY, nowSec: 0 });
  assert.equal(hold.estimate, 300000);
  assert.equal(readMapN(billingRow(ddb, "u").reserved_microcents)[TODAY], 300000);
});
test("trial reserve: denies when spent+reserved+estimate would cross cap", async () => {
  const ddb = makeFakeDdb();
  trialBilling(ddb._store, "u", { spent: { [TODAY]: 2_800_000 } }); // +300k = 3.1M > 3M
  await assert.rejects(
    reserveOrThrow({ ddb, env: ENV, ownerId: "u", plan: "trial", billingRow: billingRow(ddb, "u"), today: TODAY, nowSec: 0 }),
    (e) => e.status === 429 && e.code === "trial_daily_cap"
  );
});
test("trial reserve: expired trial → 429 expired", async () => {
  const ddb = makeFakeDdb();
  trialBilling(ddb._store, "u", { trialEnd: 100 });
  await assert.rejects(
    reserveOrThrow({ ddb, env: ENV, ownerId: "u", plan: "trial", billingRow: billingRow(ddb, "u"), today: TODAY, nowSec: 200 }),
    (e) => e.code === "trial_expired"
  );
});

// ---- settle: spent written + hold released; dedup; abort ------------------
test("settle trial: prices usage, ADDs spent, releases hold, claims requestId", async () => {
  const ddb = makeFakeDdb();
  trialBilling(ddb._store, "u", { reserved: { [TODAY]: 300000 } });
  const hold = { ownerId: "u", date: TODAY, estimate: 300000, plan: "trial" };
  const usage = { inputTokens: 1_000_000, outputTokens: 0 }; // $3.30 = 3,300,000
  const outcome = await settle({ ddb, env: ENV, hold, tier: "sonnet", requestId: "req-1", usage });
  assert.equal(outcome, "counted");
  const b = billingRow(ddb, "u");
  assert.equal(readMapN(b.trial_daily_spend_microcents)[TODAY], 3_300_000);
  assert.equal(readMapN(b.reserved_microcents)[TODAY], 0); // hold released
  assert.ok(ddb._store.dd["req-1"]); // dedup claimed
});
test("settle trial: duplicate requestId (meter already counted) releases hold, no double spend", async () => {
  const ddb = makeFakeDdb();
  trialBilling(ddb._store, "u", { reserved: { [TODAY]: 300000 } });
  ddb._store.dd = { "req-1": { request_id: { S: "req-1" }, source: { S: "meter" } } };
  const hold = { ownerId: "u", date: TODAY, estimate: 300000, plan: "trial" };
  const outcome = await settle({ ddb, env: ENV, hold, tier: "sonnet", requestId: "req-1", usage: { inputTokens: 1_000_000 } });
  assert.equal(outcome, "dup");
  const b = billingRow(ddb, "u");
  assert.equal(readMapN(b.trial_daily_spend_microcents)[TODAY] || 0, 0); // NOT counted
  assert.equal(readMapN(b.reserved_microcents)[TODAY], 0); // but hold released
});
test("settle trial: transient transaction failure claims NOTHING (meter can still count it)", async () => {
  // The atomicity guarantee: if the claim would land but the spend write fails,
  // the meter would skip the record as already-claimed → permanent undercount.
  // A TransactWriteItems makes it all-or-nothing: on a transient failure the
  // requestId is NOT claimed, so the meter auditor will count it.
  const ddb = makeFakeDdb();
  trialBilling(ddb._store, "u", { reserved: { [TODAY]: 300000 } });
  const realTx = ddb.transactWriteItems.bind(ddb);
  ddb.transactWriteItems = async () => { throw new Error("ProvisionedThroughputExceededException"); };
  const hold = { ownerId: "u", date: TODAY, estimate: 300000, plan: "trial" };
  await assert.rejects(settle({ ddb, env: ENV, hold, tier: "sonnet", requestId: "req-x", usage: { inputTokens: 1_000_000 } }));
  assert.equal(ddb._store.dd?.["req-x"], undefined); // NOT claimed → meter will count it
  assert.equal(readMapN(billingRow(ddb, "u").trial_daily_spend_microcents)[TODAY] || 0, 0); // NOT counted
  void realTx;
});

test("settle: no usage (abort) → hold released, nothing counted", async () => {
  const ddb = makeFakeDdb();
  trialBilling(ddb._store, "u", { reserved: { [TODAY]: 300000 } });
  const hold = { ownerId: "u", date: TODAY, estimate: 300000, plan: "trial" };
  const outcome = await settle({ ddb, env: ENV, hold, tier: "sonnet", requestId: "req-1", usage: null });
  assert.equal(outcome, "released_no_usage");
  assert.equal(readMapN(billingRow(ddb, "u").reserved_microcents)[TODAY], 0);
  assert.equal(ddb._store.dd?.["req-1"], undefined); // NOT claimed → meter auditor will
});

// ---- paid ----------------------------------------------------------------
test("paid reserve+settle: decrements remaining estimate then actual", async () => {
  const ddb = makeFakeDdb();
  ddb._store.fg = { u: { owner_id: { S: "u" }, remaining_microcents: { N: "1000000" }, reserved_microcents: { N: "0" } } };
  const { hold } = await reserveOrThrow({ ddb, env: ENV, ownerId: "u", plan: "paid", billingRow: { subscription_status: { S: "active" } }, fastGateRow: ddb._store.fg.u, today: TODAY, nowSec: 0 });
  assert.equal(readN(ddb._store.fg.u.reserved_microcents), 300000);
  await settle({ ddb, env: ENV, hold, tier: "sonnet", requestId: "r", usage: { inputTokens: 100000 } }); // $0.33 = 330000
  assert.equal(readN(ddb._store.fg.u.remaining_microcents), 1_000_000 - 330000);
  assert.equal(readN(ddb._store.fg.u.reserved_microcents), 0);
});
test("paid reserve: zero balance → 429 credit_exhausted", async () => {
  const ddb = makeFakeDdb();
  ddb._store.fg = { u: { owner_id: { S: "u" }, remaining_microcents: { N: "100" }, reserved_microcents: { N: "0" } } };
  await assert.rejects(
    reserveOrThrow({ ddb, env: ENV, ownerId: "u", plan: "paid", billingRow: { subscription_status: { S: "active" } }, fastGateRow: ddb._store.fg.u, today: TODAY, nowSec: 0 }),
    (e) => e.code === "credit_exhausted"
  );
});
test("paid reserve: missing fast-gate row + COMPLETE ledger → allowed", async () => {
  // The projection is written by the low-balance webhook and the backend
  // gate's refresh, so it legitimately lags a brand-new paying owner. A real
  // ledger backs them and the meter counts every call.
  const ddb = makeFakeDdb();
  const { hold } = await reserveOrThrow({
    ddb, env: ENV, ownerId: "u", plan: "paid",
    billingRow: {
      subscription_status: { S: "active" },
      metronome_customer_id: { S: "mc-1" },
      metronome_contract_id: { S: "ct-1" },
    },
    fastGateRow: null, today: TODAY, nowSec: 0,
  });
  assert.equal(hold.paid, true);
});
test("paid reserve: customer WITHOUT contract → ledger_unprovisioned", async () => {
  // The partial-provisioning state _provision persists deliberately when the
  // Stripe link fails. No contract ⇒ no commits ⇒ no balance, and
  // grant_prepaid_credits refuses it outright — so admitting here leaks
  // unmetered spend in exactly the failure this fence contains.
  const ddb = makeFakeDdb();
  await assert.rejects(
    reserveOrThrow({
      ddb, env: ENV, ownerId: "u", plan: "paid",
      billingRow: { subscription_status: { S: "active" }, metronome_customer_id: { S: "mc-1" } },
      fastGateRow: null, today: TODAY, nowSec: 0,
    }),
    (e) => e.code === "ledger_unprovisioned"
  );
});
test("paid reserve: no ledger at all → ledger_unprovisioned", async () => {
  const ddb = makeFakeDdb();
  await assert.rejects(
    reserveOrThrow({
      ddb, env: ENV, ownerId: "u", plan: "paid",
      billingRow: { subscription_status: { S: "active" } },
      fastGateRow: null, today: TODAY, nowSec: 0,
    }),
    (e) => e.code === "ledger_unprovisioned"
  );
});
test("paid reserve: MISSING billing row → ledger_unprovisioned (weakest evidence must not be the one that passes)", async () => {
  // resolvePlan treats an absent row as "paid". The previous guard skipped the
  // deny when billingRow was falsy, leaving the case with no ledger
  // information at all as the only one still admitted unmetered.
  const ddb = makeFakeDdb();
  await assert.rejects(
    reserveOrThrow({
      ddb, env: ENV, ownerId: "u", plan: "paid",
      billingRow: undefined, fastGateRow: null, today: TODAY, nowSec: 0,
    }),
    (e) => e.code === "ledger_unprovisioned"
  );
});
test("paid reserve: a DRAINED balance still reports credit_exhausted, not ledger_unprovisioned", async () => {
  // The two conditions must stay distinguishable: one is normal business, the
  // other is our provisioning breaking.
  const ddb = makeFakeDdb();
  ddb._store.fg = { u: { owner_id: { S: "u" }, remaining_microcents: { N: "0" } } };
  await assert.rejects(
    reserveOrThrow({
      ddb, env: ENV, ownerId: "u", plan: "paid",
      billingRow: { subscription_status: { S: "active" }, metronome_customer_id: { S: "mc-1" }, metronome_contract_id: { S: "ct-1" } },
      fastGateRow: ddb._store.fg.u, today: TODAY, nowSec: 0,
    }),
    (e) => e.code === "credit_exhausted"
  );
});
test("paid reserve: stale hold (reserved_updated_at past TTL) is reclaimed, not accumulated", async () => {
  const ddb = makeFakeDdb();
  // A hold leaked by a crashed settle: stamped far in the past (epoch 1000).
  ddb._store.fg = { u: { owner_id: { S: "u" }, remaining_microcents: { N: "1000000" }, reserved_microcents: { N: "300000" }, reserved_updated_at: { N: "1000" } } };
  const { hold } = await reserveOrThrow({ ddb, env: ENV, ownerId: "u", plan: "paid", billingRow: { subscription_status: { S: "active" } }, fastGateRow: ddb._store.fg.u, today: TODAY, nowSec: 0 });
  assert.equal(hold.paid, true);
  // RESET to the new estimate (300000), not ADDed to 600000.
  assert.equal(readN(ddb._store.fg.u.reserved_microcents), 300000);
  assert.ok(readN(ddb._store.fg.u.reserved_updated_at) > 1000); // re-stamped now
});
test("paid reserve: fresh hold → normal ADD behavior", async () => {
  const ddb = makeFakeDdb();
  const nowSec = Math.floor(Date.now() / 1000);
  ddb._store.fg = { u: { owner_id: { S: "u" }, remaining_microcents: { N: "1000000" }, reserved_microcents: { N: "300000" }, reserved_updated_at: { N: String(nowSec) } } };
  await reserveOrThrow({ ddb, env: ENV, ownerId: "u", plan: "paid", billingRow: { subscription_status: { S: "active" } }, fastGateRow: ddb._store.fg.u, today: TODAY, nowSec: 0 });
  assert.equal(readN(ddb._store.fg.u.reserved_microcents), 600000); // accumulated, not reset
});
test("paid reserve: fresh hold still counts against balance → denial", async () => {
  const ddb = makeFakeDdb();
  const nowSec = Math.floor(Date.now() / 1000);
  // 500k - 300k(fresh hold) - 300k(estimate) <= 0 → deny. (A STALE hold with
  // these numbers would be reclaimed and admit.)
  ddb._store.fg = { u: { owner_id: { S: "u" }, remaining_microcents: { N: "500000" }, reserved_microcents: { N: "300000" }, reserved_updated_at: { N: String(nowSec) } } };
  await assert.rejects(
    reserveOrThrow({ ddb, env: ENV, ownerId: "u", plan: "paid", billingRow: { subscription_status: { S: "active" } }, fastGateRow: ddb._store.fg.u, today: TODAY, nowSec: 0 }),
    (e) => e.code === "credit_exhausted"
  );
});
test("paid settle + release stamp reserved_updated_at", async () => {
  const ddb = makeFakeDdb();
  ddb._store.fg = { u: { owner_id: { S: "u" }, remaining_microcents: { N: "1000000" }, reserved_microcents: { N: "600000" } } };
  const hold = { ownerId: "u", estimate: 300000, plan: "paid", paid: true };
  await settle({ ddb, env: ENV, hold, tier: "sonnet", requestId: "r", usage: { inputTokens: 100000 } });
  const stamped = readN(ddb._store.fg.u.reserved_updated_at);
  assert.ok(stamped !== undefined && stamped > 0);
  await release({ ddb, env: ENV, hold });
  assert.equal(readN(ddb._store.fg.u.reserved_microcents), 0);
  assert.ok(readN(ddb._store.fg.u.reserved_updated_at) >= stamped);
});

// ---- non-streaming usage extraction (Codex finding: JSON routes must debit) --
test("extractNonStreamUsage: Converse JSON body usage", async () => {
  const { extractNonStreamUsage } = await import("./proxy.mjs");
  const body = Buffer.from(JSON.stringify({ output: {}, usage: { inputTokens: 10, outputTokens: 20, cacheReadInputTokens: 5 } }));
  assert.deepEqual(extractNonStreamUsage(body, {}), { inputTokens: 10, outputTokens: 20, cacheReadInputTokens: 5 });
});
test("extractNonStreamUsage: Anthropic invoke body (snake_case) prices via normalizeUsage", async () => {
  const { extractNonStreamUsage } = await import("./proxy.mjs");
  const body = Buffer.from(JSON.stringify({ content: [], usage: { input_tokens: 7, output_tokens: 9, cache_read_input_tokens: 3, cache_creation_input_tokens: 2 } }));
  const u = extractNonStreamUsage(body, {});
  assert.deepEqual(normalizeUsage(u), { inputTokens: 7, outputTokens: 9, cacheReadTokens: 3, cacheWriteTokens: 2 });
});
test("extractNonStreamUsage: falls back to bedrock token-count headers", async () => {
  const { extractNonStreamUsage } = await import("./proxy.mjs");
  const u = extractNonStreamUsage(Buffer.from("not json"), { "x-amzn-bedrock-input-token-count": "11", "x-amzn-bedrock-output-token-count": "13" });
  assert.deepEqual(normalizeUsage(u), { inputTokens: 11, outputTokens: 13, cacheReadTokens: 0, cacheWriteTokens: 0 });
});
test("extractNonStreamUsage: no usage anywhere -> null (hold released, meter backfills)", async () => {
  const { extractNonStreamUsage } = await import("./proxy.mjs");
  assert.equal(extractNonStreamUsage(Buffer.from("{}"), {}), null);
});

// ---- normalizeUsage: converse vs invoke shapes ---------------------------
test("normalizeUsage handles both shapes", () => {
  assert.deepEqual(normalizeUsage({ inputTokens: 5, outputTokens: 7, cacheReadInputTokens: 2, cacheWriteInputTokens: 3 }),
    { inputTokens: 5, outputTokens: 7, cacheReadTokens: 2, cacheWriteTokens: 3 });
  assert.deepEqual(normalizeUsage({ inputTokenCount: 5, outputTokenCount: 7 }),
    { inputTokens: 5, outputTokens: 7, cacheReadTokens: 0, cacheWriteTokens: 0 });
  assert.equal(normalizeUsage(null), null);
});
