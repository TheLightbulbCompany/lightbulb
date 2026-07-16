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

/**
 * Settle a trial invocation: claim the requestId (dedup, shared with the meter),
 * and on a fresh claim ADD `actual` to the authoritative spent counter AND
 * release the hold, in ONE UpdateItem. If the claim loses (meter counted it —
 * retries only), release the hold but do NOT add spend.
 * @returns {"counted"|"dup"}
 */
export async function trialSettle(ddb, env, hold, requestId, actual, ttlSeconds) {
  let claimed = false;
  try {
    await ddb.putItem({
      TableName: env.INVOCATION_DEDUP_TABLE,
      Item: { request_id: { S: requestId }, source: { S: "gateway" }, ttl: N(Math.floor(Date.now() / 1000) + ttlSeconds) },
      ConditionExpression: "attribute_not_exists(request_id)",
    });
    claimed = true;
  } catch (e) {
    if (!isConditionFail(e)) throw e;
  }
  await ensureTrialMaps(ddb, env.BILLING_TABLE, hold.ownerId);
  if (claimed) {
    await ddb.updateItem({
      TableName: env.BILLING_TABLE,
      Key: { owner_id: { S: hold.ownerId } },
      UpdateExpression:
        "SET trial_daily_spend_microcents.#d = if_not_exists(trial_daily_spend_microcents.#d, :zero) + :act, " +
        "reserved_microcents.#d = if_not_exists(reserved_microcents.#d, :zero) - :est",
      ExpressionAttributeNames: { "#d": hold.date },
      ExpressionAttributeValues: { ":act": N(actual), ":est": N(hold.estimate), ":zero": N(0) },
    });
    return "counted";
  }
  await trialRelease(ddb, env, hold); // meter owns the spend; just free the hold
  return "dup";
}

// ---- paid: scalar balance on the fast-gate row ---------------------------
// The gate's paid debit is an ESTIMATE that self-corrects — Metronome remains
// the billing truth (the meter Lambda is its sole writer) and the fast-gate
// refresh periodically overwrites `remaining_microcents`. So paid needs no
// dedup: a double-count is transient, erased on the next refresh.

export async function paidReserve(ddb, env, ownerId, estimate, fastGateRow) {
  if (!fastGateRow) return { ok: true, hold: { ownerId, estimate, paid: true } }; // no row → allow (backstopped)
  let remaining = readN(fastGateRow.remaining_microcents) ?? 0;
  let reserved = readN(fastGateRow.reserved_microcents) ?? 0;
  for (let attempt = 0; attempt < MAX_RESERVE_ATTEMPTS; attempt++) {
    if (remaining - reserved - estimate <= 0) return { ok: false };
    try {
      await ddb.updateItem({
        TableName: env.FAST_GATE_TABLE,
        Key: { owner_id: { S: ownerId } },
        UpdateExpression: "SET reserved_microcents = if_not_exists(reserved_microcents, :zero) + :est",
        ConditionExpression: "attribute_not_exists(reserved_microcents) OR reserved_microcents = :seen",
        ExpressionAttributeValues: { ":est": N(estimate), ":zero": N(0), ":seen": N(reserved) },
      });
      return { ok: true, hold: { ownerId, estimate, paid: true } };
    } catch (e) {
      if (!isConditionFail(e)) throw e;
      const fresh = await ddb.getItem({ TableName: env.FAST_GATE_TABLE, Key: { owner_id: { S: ownerId } } });
      remaining = readN(fresh?.Item?.remaining_microcents) ?? 0;
      reserved = readN(fresh?.Item?.reserved_microcents) ?? 0;
    }
  }
  return { ok: false };
}

export async function paidRelease(ddb, env, hold) {
  try {
    await ddb.updateItem({
      TableName: env.FAST_GATE_TABLE,
      Key: { owner_id: { S: hold.ownerId } },
      UpdateExpression: "SET reserved_microcents = if_not_exists(reserved_microcents, :zero) - :est",
      ConditionExpression: "attribute_exists(owner_id)",
      ExpressionAttributeValues: { ":est": N(hold.estimate), ":zero": N(0) },
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
        "reserved_microcents = if_not_exists(reserved_microcents, :zero) - :est",
      ConditionExpression: "attribute_exists(owner_id)",
      ExpressionAttributeValues: { ":act": N(actual), ":est": N(hold.estimate), ":zero": N(0) },
    });
  } catch (e) {
    if (!isConditionFail(e)) throw e; // no fast-gate row → nothing to debit (allowed path)
  }
}
