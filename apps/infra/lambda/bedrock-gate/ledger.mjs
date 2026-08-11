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
  // Ledger completeness is checked FIRST, for BOTH paths — not just the
  // missing-projection one.
  //
  // A cached projection is not evidence that a ledger still exists. The row is
  // written by the low-balance webhook and the backend gate's refresh, and
  // NOTHING deletes it when the Metronome customer/contract goes away (an
  // account wipe archives the customer; the projection survives). A positive
  // remaining_microcents left behind that way would otherwise take the
  // cached-balance path below, skip this fence entirely, and keep admitting —
  // AND keep decrementing a balance that no longer maps to anything billable.
  // The stale-row path is no safer: it re-reads the same orphaned row.
  //
  // Free to hoist: core.mjs already fetches the billing row unconditionally
  // for resolvePlan, so this costs no extra DynamoDB call on any request.
  //
  // Both halves are required. When the Stripe-link step fails,
  // metronome_provisioning._provision deliberately persists ONLY
  // metronome_customer_id and defers the contract (its billing config is
  // create-time-only, so creating it unlinked would strand the owner's top-ups
  // permanently). No contract means no prepaid commits, hence no balance —
  // grant_prepaid_credits refuses that state outright.
  //
  // A MISSING billing row denies too: it is the weakest possible evidence of a
  // ledger. Gate tokens are minted only at provision time, after the row
  // exists (ecs_manager), so a token-holding owner with no row is anomalous,
  // not a normal startup state.
  //
  // billingRow is the RAW DynamoDB item (core.mjs passes getItem's .Item
  // straight through), so these are attribute-value wrappers — presence is the
  // whole test and `{S: "..."}` is truthy. Do NOT "fix" this into readS(...):
  // that changes behavior on an empty-string value.
  //
  // The backend gate (is_owner_blocked) shares this DEFINITION of a complete
  // ledger but deliberately not this evaluation order: it checks only on its
  // cold path, because its equivalent lookup is an extra DynamoDB read on
  // every chat message and it is a UX gate, not the money fence. THIS is the
  // fence — cron- and channel-triggered runs reach Bedrock through here with
  // no backend involvement — so it pays the (zero) cost of checking always.
  if (!(billingRow && billingRow.metronome_customer_id && billingRow.metronome_contract_id)) {
    return { ok: false, unprovisioned: true };
  }

  if (!fastGateRow) {
    // Ledger is complete (checked above) but the projection has not been
    // written yet — it is created by the low-balance webhook and the backend
    // gate's refresh, so it legitimately lags a brand-new paying owner. A real
    // ledger backs them and the meter's Metronome ingestion counts every call,
    // so denying here would hard-block their first request.
    //
    // This replaces "no row → allow (backstopped)", whose premise was false:
    // the meter's backstop for a PAID owner IS Metronome ingestion, which
    // cannot record without a ledger to record against. Every call was
    // therefore admitted unmetered — Opus included, since the paid AIP map
    // carries all three tiers.
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
    // leaked hold would suppress admission FOREVER. reserved_updated_at tracks
    // the age of the OLDEST outstanding hold: it is stamped only when the
    // reserved balance goes from 0 → positive (a hold is born) or on reclaim,
    // and is deliberately NOT re-stamped by later holds, releases, or settles
    // (see paidRelease/paidSettle). If it were re-stamped on every write, a
    // busy owner's leaked hold would never age past the TTL and would suppress
    // its balance forever. A hold older than the TTL (or a positive reserved
    // with no stamp) is treated as leaked and RECLAIMED — reset to this
    // request's estimate instead of accumulated — so the TTL bounds how long a
    // leak can shrink the admittable balance even under continuous load.
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
        // Stamp reserved_updated_at ONLY on the first outstanding hold
        // (reserved 0 → positive). While reserved stays positive, leave the
        // oldest hold's stamp in place so a leak keeps aging.
        const firstHold = reserved === 0;
        const values = { ":est": N(estimate), ":zero": N(0), ":seen": N(reserved) };
        if (firstHold) values[":now"] = N(now);
        await ddb.updateItem({
          TableName: env.FAST_GATE_TABLE,
          Key: { owner_id: { S: ownerId } },
          UpdateExpression:
            "SET reserved_microcents = if_not_exists(reserved_microcents, :zero) + :est" +
            (firstHold ? ", reserved_updated_at = :now" : ""),
          ConditionExpression: "attribute_not_exists(reserved_microcents) OR reserved_microcents = :seen",
          ExpressionAttributeValues: values,
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
  // Does NOT touch reserved_updated_at: that stamp tracks the OLDEST
  // outstanding hold (stamped at 0 → positive in paidReserve). Re-stamping on
  // release would reset a busy owner's leak clock and hide leaked holds forever.
  try {
    await ddb.updateItem({
      TableName: env.FAST_GATE_TABLE,
      Key: { owner_id: { S: hold.ownerId } },
      UpdateExpression:
        "SET reserved_microcents = if_not_exists(reserved_microcents, :zero) - :est",
      ConditionExpression: "attribute_exists(owner_id)",
      ExpressionAttributeValues: { ":est": N(hold.estimate), ":zero": N(0) },
    });
  } catch (e) {
    if (!isConditionFail(e)) throw e; // no row (never reserved) — nothing to release
  }
}

export async function paidSettle(ddb, env, hold, actual) {
  // Like paidRelease, deliberately does NOT re-stamp reserved_updated_at — see
  // paidReserve for why the stamp must track only the oldest hold's age.
  try {
    await ddb.updateItem({
      TableName: env.FAST_GATE_TABLE,
      Key: { owner_id: { S: hold.ownerId } },
      UpdateExpression:
        "SET remaining_microcents = if_not_exists(remaining_microcents, :zero) - :act, " +
        "reserved_microcents = if_not_exists(reserved_microcents, :zero) - :est",
      ConditionExpression: "attribute_exists(owner_id)",
      ExpressionAttributeValues: { ":act": N(actual), ":est": N(hold.estimate), ":zero": N(0) },
    });
  } catch (e) {
    if (!isConditionFail(e)) throw e; // no fast-gate row → nothing to debit (allowed path)
  }
}
