import { test } from "node:test";
import assert from "node:assert/strict";
import { makeFakeDdb } from "./fake-ddb.mjs";
import { hashToken } from "./auth.mjs";
import { handleRequest } from "./core.mjs";

const ENV = {
  AWS_REGION: "us-east-1", GATE_ENV: "test",
  TOKENS_TABLE: "tok", BILLING_TABLE: "bill", FAST_GATE_TABLE: "fg", INVOCATION_DEDUP_TABLE: "dd",
  CAP_MICROCENTS: "3000000", RESERVE_ESTIMATE_MICROCENTS: "300000", DEDUP_TTL_SECONDS: "1296000",
};
const AIP = "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/abc123";
const NOW = Date.parse("2026-07-15T12:00:00Z");
const TODAY = "2026-07-15";

// `ledger` seeds the Metronome customer+contract ids. Default TRUE because
// paidReserve now checks ledger completeness on every path, and any paid owner
// exercising these flows has a ledger in reality — a trial owner never reaches
// that check (resolvePlan sends them to trialReserve). Pass ledger:false to
// exercise the unprovisioned deny.
function seed({ denied = false, spent = 0, status = "trial_local", ledger = true, providerChoice } = {}) {
  const ddb = makeFakeDdb();
  ddb._store.tok = { [hashToken("t1")]: { token_hash: { S: hashToken("t1") }, owner_id: { S: "u" }, denied: { BOOL: denied }, aip_tiers: { M: { [AIP]: { S: "sonnet" } } } } };
  ddb._store.bill = { u: { owner_id: { S: "u" }, subscription_status: { S: status }, trial_daily_spend_microcents: { M: { [TODAY]: { N: String(spent) } } }, reserved_microcents: { M: {} }, ...(providerChoice ? { provider_choice: { S: providerChoice } } : {}), ...(ledger ? { metronome_customer_id: { S: "mc-1" }, metronome_contract_id: { S: "ct-1" } } : {}) } };
  return ddb;
}
function makeResponder() {
  const r = { _status: null, _headers: null, _chunks: [], _ended: false, _sent: false, _errBody: null };
  return Object.assign(r, {
    headersSent: () => r._sent,
    writeHead: (s, h) => { r._sent = true; r._status = s; r._headers = h; },
    write: (c) => r._chunks.push(Buffer.from(c)),
    end: () => { r._ended = true; },
    error: (e) => { r._sent = true; r._status = e.status; r._errBody = JSON.parse(e.body()); },
    abortSignal: undefined,
  });
}
const evt = (op = "converse-stream", tokenOk = true, model = AIP) => ({
  requestContext: { http: { method: "POST", path: `/model/${encodeURIComponent(model)}/${op}` } },
  rawPath: `/model/${encodeURIComponent(model)}/${op}`,
  headers: tokenOk ? { authorization: "Bearer t1" } : {},
  body: JSON.stringify({ messages: [{ role: "user", content: [{ text: "hi" }] }] }),
});

const fakeForward200 = (usage, requestId = "req-1") => async ({ writeHead, write, end }) => {
  writeHead(200, { "content-type": "application/vnd.amazon.eventstream", "x-amzn-requestid": requestId });
  write(Buffer.from("STREAMBYTES"));
  end();
  return { status: 200, requestId, usage };
};

// Bedrock admits only bedrock_claude owners (paid), so the budget/hold paths run on the fast-gate row.
const paid = (remaining = 1_000_000) => {
  const ddb = seed({ status: "active" });
  ddb._store.fg = { u: { owner_id: { S: "u" }, remaining_microcents: { N: String(remaining) }, reserved_microcents: { N: "0" } } };
  return ddb;
};

test("no balance: 429 BEFORE forward (forward never called)", async () => {
  const ddb = paid(0);
  const r = makeResponder();
  let forwardCalled = false;
  await handleRequest(evt(), r, { ddb, env: ENV, nowMs: NOW, forwardImpl: async () => { forwardCalled = true; } });
  assert.equal(forwardCalled, false);
  assert.equal(r._status, 429);
  assert.equal(r._errBody.__type, "ThrottlingException");
});

test("unknown token → 401 (no forward, no hold)", async () => {
  const ddb = seed();
  const r = makeResponder();
  await handleRequest(evt("converse-stream", false), r, { ddb, env: ENV, nowMs: NOW, forwardImpl: async () => { throw new Error("should not run"); } });
  assert.equal(r._status, 401);
});

test("foreign AIP → 403 (cross-tenant blocked)", async () => {
  const ddb = seed();
  const r = makeResponder();
  const other = "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/other";
  await handleRequest(evt("converse-stream", true, other), r, { ddb, env: ENV, nowMs: NOW, forwardImpl: async () => { throw new Error("no"); } });
  assert.equal(r._status, 403);
});

test("bad route → 404", async () => {
  const ddb = seed();
  const r = makeResponder();
  await handleRequest({ requestContext: { http: { method: "GET", path: "/health" } }, rawPath: "/health", headers: {} }, r, { ddb, env: ENV, nowMs: NOW });
  assert.equal(r._status, 404);
});

test("upstream non-200 passes through; hold released, no spend", async () => {
  const ddb = paid();
  const r = makeResponder();
  const forward403 = async ({ writeHead, write, end }) => { writeHead(403, { "content-type": "application/json" }); write(Buffer.from('{"__type":"AccessDeniedException"}')); end(); return { status: 403 }; };
  await handleRequest(evt(), r, { ddb, env: ENV, nowMs: NOW, forwardImpl: forward403 });
  assert.equal(r._status, 403);
  assert.equal(Number(ddb._store.fg.u.remaining_microcents.N), 1_000_000);
  assert.equal(Number(ddb._store.fg.u.reserved_microcents.N), 0); // hold released
});

test("forward throws AFTER reserve → hold released in finally", async () => {
  const ddb = paid();
  const r = makeResponder();
  await handleRequest(evt(), r, { ddb, env: ENV, nowMs: NOW, forwardImpl: async () => { throw new Error("network"); } });
  assert.equal(Number(ddb._store.fg.u.reserved_microcents.N), 0); // released
  assert.equal(r._status, 502); // headers not sent → internal error surfaced
});

test("paid owner streams + decrements fast-gate", async () => {
  const ddb = seed({ status: "active" });
  ddb._store.fg = { u: { owner_id: { S: "u" }, remaining_microcents: { N: "1000000" }, reserved_microcents: { N: "0" } } };
  const r = makeResponder();
  await handleRequest(evt(), r, { ddb, env: ENV, nowMs: NOW, forwardImpl: fakeForward200({ inputTokens: 100000 }) });
  assert.equal(r._status, 200);
  assert.equal(Number(ddb._store.fg.u.remaining_microcents.N), 1_000_000 - 330000);
});

test("paid owner switched to OpenRouter cannot invoke Bedrock before reservation", async () => {
  const ddb = seed({ status: "active", providerChoice: "openrouter_managed" });
  ddb._store.fg = { u: { owner_id: { S: "u" }, remaining_microcents: { N: "1000000" }, reserved_microcents: { N: "0" } } };
  const r = makeResponder();
  let forwarded = false;
  await handleRequest(evt(), r, { ddb, env: ENV, nowMs: NOW, forwardImpl: async () => { forwarded = true; } });
  assert.equal(r._status, 403);
  assert.equal(forwarded, false);
  assert.equal(ddb._store.fg.u.reserved_microcents.N, "0");
});

for (const [name, row] of [
  ["trial_local", { status: "trial_local" }],
  ["choiceless Base", { status: "active", plan: "base" }],
]) {
  test(`${name} owner is refused Bedrock with no ledger write`, async () => {
    const ddb = seed(row);
    if (row.plan) ddb._store.bill.u.plan = { S: row.plan };
    const before = JSON.stringify(ddb._store.bill.u);
    const r = makeResponder();
    let forwarded = false;
    await handleRequest(evt(), r, { ddb, env: ENV, nowMs: NOW, forwardImpl: async () => { forwarded = true; } });
    assert.equal(r._status, 403);
    assert.equal(forwarded, false);
    assert.equal(JSON.stringify(ddb._store.bill.u), before);
  });
}

test("explicit bedrock_claude owner is admitted", async () => {
  const ddb = seed({ status: "active", providerChoice: "bedrock_claude" });
  ddb._store.fg = { u: { owner_id: { S: "u" }, remaining_microcents: { N: "1000000" }, reserved_microcents: { N: "0" } } };
  const r = makeResponder();
  await handleRequest(evt(), r, { ddb, env: ENV, nowMs: NOW, forwardImpl: fakeForward200({ inputTokens: 0 }) });
  assert.equal(r._status, 200);
});

test("paid owner with NO ledger is denied end-to-end with ledger_unprovisioned", async () => {
  // The unit tests cover paidReserve directly; this pins the whole request
  // path — a paid owner whose Metronome ledger was never provisioned must be
  // refused BEFORE anything is forwarded to Bedrock, and must be
  // distinguishable from a customer who simply ran out of credits.
  const ddb = seed({ status: "active", ledger: false });
  ddb._store.fg = { u: { owner_id: { S: "u" }, remaining_microcents: { N: "1000000" }, reserved_microcents: { N: "0" } } };
  const r = makeResponder();
  let forwarded = false;
  await handleRequest(evt(), r, {
    ddb, env: ENV, nowMs: NOW,
    forwardImpl: async () => { forwarded = true; return fakeForward200({ inputTokens: 1 })(); },
  });
  assert.equal(r._status, 429);
  assert.equal(forwarded, false);
  // A positive cached projection must NOT rescue an owner with no ledger —
  // nothing deletes the row when the Metronome customer goes away.
  assert.equal(Number(ddb._store.fg.u.remaining_microcents.N), 1_000_000);
});
