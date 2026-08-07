// The budget ledger — the money path. Two counters per owner:
//   spent    — authoritative actuals (trial: billing_accounts
//              trial_daily_spend_microcents.{date}, the SAME field + expression
//              the meter Lambda writes; paid: fast-gate remaining, decremented).
//   reserved — outstanding pre-flight holds (ephemeral; released on settle/abort).
//
// Admission is gated on spent + reserved + estimate <= cap. spent and reserved
// are separate attributes and DynamoDB cannot condition on their sum, so we read
// a snapshot, check locally, then reserve with a CONDITIONAL add that fails if
// `reserved` changed since the snapshot. Every concurrent operation that affects
// the budget (a competing admit OR a settle) touches `reserved`, so guarding on
// its exact value detects them all — preventing two concurrent requests from
// both reserving against the same stale snapshot (which would over-ADMIT, the
// unsafe direction). On a guard miss we re-read and retry a few times, then deny
// (busy → safe deny). Over-reserving is the safe direction (denies earlier,
// never overspends); we only guard to stop concurrent over-admission.

import { N, readN, readMapN } from "./ddb.mjs";

const MAX_RESERVE_ATTEMPTS = 4;

class ConditionFailed extends Error {}
function isConditionFail(e) {
  return e?.name === "ConditionalCheckFailedException" || e instanceof ConditionFailed;
}

// ---- trial: per-day map on billing_accounts ------------------------------

async function ensureTrialMaps(ddb, table, ownerId) {
  // ADD into a nested map path fails if the parent map is absent. Initialize
  // both maps if missing (idempotent, guarded so we never clobber existing).
  for (const attr of ["trial_daily_spend_microcents", "reserved_microcents"]) {
    try {
      await ddb.updateItem({
        TableName: table,
        Key: { owner_id: { S: ownerId } },
        UpdateExpression: `SET ${attr} = :empty`,
        ConditionExpression: `attribute_exists(owner_id) AND attribute_not_exists(${attr})`,
        ExpressionAttributeValues: { ":empty": { M: {} } },
      });
    } catch (e) {
      if (!isConditionFail(e)) throw e; // already exists (or no row) — fine
    }
  }
}

/**
 * Reserve `estimate` for a trial owner if spent+reserved+estimate <= cap.
 * @returns {{ok:true, hold:{ownerId,date,estimate}} | {ok:false}}
 */
export async function trialReserve(ddb, env, ownerId, date, estimate, cap, billingRow) {
  // No staleness TTL here (unlike paidReserve): the reserved map is date-keyed,
  // so a leaked trial hold self-heals at the next UTC rollover.
  await ensureTrialMaps(ddb, env.BILLING_TABLE, ownerId);
  let spentMap = readMapN(billingRow?.trial_daily_spend_microcents);
  let reservedMap = readMapN(billingRow?.reserved_microcents);
  for (let attempt = 0; attempt < MAX_RESERVE_ATTEMPTS; attempt++) {
    const spent = spentMap[date] || 0;
    const reserved = reservedMap[date] || 0;
    if (spent + reserved + estimate > cap) return { ok: false };
    try {
      await ddb.updateItem({
        TableName: env.BILLING_TABLE,
        Key: { owner_id: { S: ownerId } },
        UpdateExpression:
          "SET reserved_microcents.#d = if_not_exists(reserved_microcents.#d, :zero) + :est",
        // Guard: reserved for this day is still what we snapshotted (or absent).
        ConditionExpression:
          "attribute_not_exists(reserved_microcents.#d) OR reserved_microcents.#d = :seen",
        ExpressionAttributeNames: { "#d": date },
        ExpressionAttributeValues: {
          ":est": N(estimate),
          ":zero": N(0),
          ":seen": N(reserved),
        },
      });
      return { ok: true, hold: { ownerId, date, estimate } };
    } catch (e) {
      if (!isConditionFail(e)) throw e;
      // Concurrent change — re-read the row and retry against fresh values.
      const fresh = await ddb.getItem({ TableName: env.BILLING_TABLE, Key: { owner_id: { S: ownerId } } });
      spentMap = readMapN(fresh?.Item?.trial_daily_spend_microcents);
      reservedMap = readMapN(fresh?.Item?.reserved_microcents);
    }
  }
  return { ok: false }; // contended → safe deny
}

/** Release a trial hold (best-effort; never throws to the caller path). */
export async function trialRelease(ddb, env, hold) {
  await ddb.updateItem({
    TableName: env.BILLING_TABLE,
    Key: { owner_id: { S: hold.ownerId } },
    UpdateExpression: "SET reserved_microcents.#d = if_not_exists(reserved_microcents.#d, :zero) - :est",
    ExpressionAttributeNames: { "#d": hold.date },
    ExpressionAttributeValues: { ":est": N(hold.estimate), ":zero": N(0) },
  });
}

function isTransactionConditionFail(e) {
  if (e?.name === "TransactionCanceledException") {
    const reasons = e.CancellationReasons || [];
    return reasons.some((r) => r?.Code === "ConditionalCheckFailed");
  }
  return isConditionFail(e);
}

/**
 * Settle a trial invocation ATOMICALLY: claim the requestId (dedup, shared with
 * the meter) AND add `actual` to the authoritative spent counter AND release the
 * hold, in ONE TransactWriteItems. Atomicity is load-bearing: if the claim
 * landed but the spend write didn't, the meter auditor would see the record as
 * already-claimed and skip it — permanently undercounting the invocation. With
 * the transaction, either the claim+spend both land ("counted"), or nothing
 * does. A condition failure means the meter already claimed it (retries only) →
 * release the hold, no double spend ("dup"). A transient transaction failure
 * writes nothing → the caller's finally releases the hold and the meter counts
 * it (its claim will succeed).
 * @returns {"counted"|"dup"}
 */
export async function trialSettle(ddb, env, hold, requestId, actual, ttlSeconds) {
  // Nested-map ADD needs the parent maps present; ensure them outside the
  // transaction (idempotent, guarded — never clobbers existing values).
  await ensureTrialMaps(ddb, env.BILLING_TABLE, hold.ownerId);
  try {
    await ddb.transactWriteItems({
      TransactItems: [
        {
          Put: {
            TableName: env.INVOCATION_DEDUP_TABLE,
            Item: { request_id: { S: requestId }, source: { S: "gateway" }, ttl: N(Math.floor(Date.now() / 1000) + ttlSeconds) },
            ConditionExpression: "attribute_not_exists(request_id)",
          },
        },
        {
          Update: {
            TableName: env.BILLING_TABLE,
            Key: { owner_id: { S: hold.ownerId } },
            UpdateExpression:
              "SET trial_daily_spend_microcents.#d = if_not_exists(trial_daily_spend_microcents.#d, :zero) + :act, " +
              "reserved_microcents.#d = if_not_exists(reserved_microcents.#d, :zero) - :est",
            ExpressionAttributeNames: { "#d": hold.date },
            ExpressionAttributeValues: { ":act": N(actual), ":est": N(hold.estimate), ":zero": N(0) },
          },
        },
      ],
    });
    return "counted";
  } catch (e) {
    if (!isTransactionConditionFail(e)) throw e; // transient → nothing written; finally releases; meter counts
    await trialRelease(ddb, env, hold); // meter already owns the spend; just free the hold
    return "dup";
  }
}

// ---- paid: scalar balance on the fast-gate row ---------------------------
// The gate's paid debit is an ESTIMATE that self-corrects — Metronome remains
// the billing truth (the meter Lambda is its sole writer) and the fast-gate
// refresh periodically overwrites `remaining_microcents`. So paid needs no
// dedup: a double-count is transient, erased on the next refresh.

export async function paidReserve(ddb, env, ownerId, estimate, fastGateRow, billingRow) {
  if (!fastGateRow) {
    // No projection row. Whether that is benign depends entirely on whether a
    // LEDGER exists behind it:
    //
    //   complete ledger (customer AND contract) → allow. The projection is
    //     just not written yet — it is created by the low-balance webhook and
    //     by the backend gate's refresh. A real ledger backs this owner and
    //     the meter's Metronome ingestion counts every call, so denying would
    //     hard-block a paying customer's first request.
    //
    //   anything else → DENY. Nothing can be debited. The "no row → allow
    //     (backstopped)" this replaces was false: the meter's backstop for a
    //     PAID owner is Metronome ingestion, which cannot record without a
    //     ledger to record against, so every call was admitted unmetered —
    //     Opus included, since the paid AIP map carries all three tiers.
    //
    // Both halves of the ledger are required. When the Stripe-link step fails,
    // metronome_provisioning._provision deliberately persists ONLY
    // metronome_customer_id and defers the contract (its billing config is
    // create-time-only, so creating it unlinked would strand the owner's
    // top-ups permanently). No contract means no prepaid commits, hence no
    // balance — grant_prepaid_credits refuses that state outright. Testing the
    // customer alone would admit unmetered traffic in exactly the
    // partial-provisioning failure this fence exists to contain.
    //
    // A MISSING billing row denies too. It is the weakest possible evidence of
    // a ledger, and the previous guard skipped the deny for it — leaving the
    // one case with no ledger information at all as the one still admitted.
    // Gate tokens are minted only at provision time, after the billing row
    // exists (ecs_manager), so a token-holding owner with no row is anomalous,
    // not a normal startup state.
    //
    // billingRow is the RAW DynamoDB item (core.mjs passes getItem's .Item
    // straight through), so these are attribute-value wrappers — presence is
    // the whole test and `{S: "..."}` is truthy. Do NOT "fix" this into
    // readS(...): that changes behavior on an empty-string value.
    //
    // The backend gate applies the same completeness test, but it only covers
    // doors the backend sits on. A cron- or channel-triggered run inside the
    // container reaches Bedrock through HERE with no backend involvement, so
    // this is the fence that actually has to hold.
    const ledgerComplete = Boolean(
      billingRow && billingRow.metronome_customer_id && billingRow.metronome_contract_id,
    );
    if (!ledgerComplete) return { ok: false, unprovisioned: true };
    return { ok: true, hold: { ownerId, estimate, paid: true } };
  }
  const ttlSec = Number(env.RESERVE_TTL_SECONDS || 1200);
  let remaining = readN(fastGateRow.remaining_microcents) ?? 0;
  let reserved = readN(fastGateRow.reserved_microcents) ?? 0;
  let reservedAt = readN(fastGateRow.reserved_updated_at);
  for (let attempt = 0; attempt < MAX_RESERVE_ATTEMPTS; attempt++) {
    const now = Math.floor(Date.now() / 1000);
    // Unlike the trial map, the paid hold is a scalar with no date rollover to
    // self-heal it: if a Lambda crashed between reserve and settle/release, the
    // leaked hold would suppress admission FOREVER. Every hold-changing write
    // stamps reserved_updated_at; a hold older than the TTL (or with no stamp)
    // is treated as leaked and RECLAIMED — reset to this request's estimate
    // instead of accumulated — so the TTL bounds how long a leak can shrink
    // the admittable balance.
    const stale = reserved > 0 && (reservedAt === undefined || now - reservedAt > ttlSec);
    const effectiveReserved = stale ? 0 : reserved;
    if (remaining - effectiveReserved - estimate <= 0) return { ok: false };
    try {
      if (stale) {
        // Reclaim, guarded on the exact stale value so a concurrent
        // reserve/settle (which changes reserved_microcents) is detected.
        await ddb.updateItem({
          TableName: env.FAST_GATE_TABLE,
          Key: { owner_id: { S: ownerId } },
          UpdateExpression: "SET reserved_microcents = :est, reserved_updated_at = :now",
          ConditionExpression: "reserved_microcents = :seen",
          ExpressionAttributeValues: { ":est": N(estimate), ":seen": N(reserved), ":now": N(now) },
        });
      } else {
        await ddb.updateItem({
          TableName: env.FAST_GATE_TABLE,
          Key: { owner_id: { S: ownerId } },
          UpdateExpression:
            "SET reserved_microcents = if_not_exists(reserved_microcents, :zero) + :est, reserved_updated_at = :now",
          ConditionExpression: "attribute_not_exists(reserved_microcents) OR reserved_microcents = :seen",
          ExpressionAttributeValues: { ":est": N(estimate), ":zero": N(0), ":seen": N(reserved), ":now": N(now) },
        });
      }
      return { ok: true, hold: { ownerId, estimate, paid: true } };
    } catch (e) {
      if (!isConditionFail(e)) throw e;
      const fresh = await ddb.getItem({ TableName: env.FAST_GATE_TABLE, Key: { owner_id: { S: ownerId } } });
      remaining = readN(fresh?.Item?.remaining_microcents) ?? 0;
      reserved = readN(fresh?.Item?.reserved_microcents) ?? 0;
      reservedAt = readN(fresh?.Item?.reserved_updated_at);
    }
  }
  return { ok: false };
}

export async function paidRelease(ddb, env, hold) {
  try {
    await ddb.updateItem({
      TableName: env.FAST_GATE_TABLE,
      Key: { owner_id: { S: hold.ownerId } },
      UpdateExpression:
        "SET reserved_microcents = if_not_exists(reserved_microcents, :zero) - :est, reserved_updated_at = :now",
      ConditionExpression: "attribute_exists(owner_id)",
      ExpressionAttributeValues: { ":est": N(hold.estimate), ":zero": N(0), ":now": N(Math.floor(Date.now() / 1000)) },
    });
  } catch (e) {
    if (!isConditionFail(e)) throw e; // no row (never reserved) — nothing to release
  }
}

export async function paidSettle(ddb, env, hold, actual) {
  try {
    await ddb.updateItem({
      TableName: env.FAST_GATE_TABLE,
      Key: { owner_id: { S: hold.ownerId } },
      UpdateExpression:
        "SET remaining_microcents = if_not_exists(remaining_microcents, :zero) - :act, " +
        "reserved_microcents = if_not_exists(reserved_microcents, :zero) - :est, " +
        "reserved_updated_at = :now",
      ConditionExpression: "attribute_exists(owner_id)",
      ExpressionAttributeValues: { ":act": N(actual), ":est": N(hold.estimate), ":zero": N(0), ":now": N(Math.floor(Date.now() / 1000)) },
    });
  } catch (e) {
    if (!isConditionFail(e)) throw e; // no fast-gate row → nothing to debit (allowed path)
  }
}
