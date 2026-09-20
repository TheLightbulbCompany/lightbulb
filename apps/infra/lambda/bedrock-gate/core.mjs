// Request orchestration, transport-agnostic so it unit-tests without awslambda:
// auth → AIP ownership → clamp → reserve → forward (verbatim) → settle, with a
// finally-release that always runs. handler.mjs wraps this with
// awslambda.streamifyResponse + the real DynamoDB client.
import { handleZen } from "./zen.mjs";
import { readS } from "./ddb.mjs";
import { authenticate, tierForModel } from "./auth.mjs";
import { resolvePlan, clampMaxTokens, reserveOrThrow } from "./preflight.mjs";
import { settle, release } from "./debit.mjs";
import { forward } from "./proxy.mjs";
import { MODEL_MAX_TOKENS } from "./rates.mjs";
import { emit } from "./metrics.mjs";
import { GateError, notFound, forbiddenOp, internal } from "./errors.mjs";

const OPS = new Set(["converse", "converse-stream", "invoke", "invoke-with-response-stream"]);

// /model/{urlEncodedModelId}/{op}
function parseRoute(method, rawPath) {
  if (method !== "POST") return null;
  const m = /^\/model\/([^/]+)\/([^/?]+)/.exec(rawPath || "");
  if (!m) return null;
  const op = m[2];
  if (!OPS.has(op)) return null;
  return { modelId: decodeURIComponent(m[1]), rawModelId: m[1], op };
}

function eventBody(event) {
  if (event.body == null) return Buffer.alloc(0);
  return event.isBase64Encoded ? Buffer.from(event.body, "base64") : Buffer.from(event.body, "utf8");
}

/**
 * @param responder {{ error(GateError):void, writeHead(status,headers):void, write(buf):void, end():void, headersSent():boolean }}
 * @param deps {{ ddb, env, nowMs?, forwardImpl? }}
 */
export async function handleRequest(event, responder, deps) {
  const { ddb, env } = deps;
  const now = deps.nowMs ?? Date.now();
  const forwardImpl = deps.forwardImpl ?? forward;
  const headers = event.headers || {};
  const method = event.requestContext?.http?.method || event.httpMethod;
  const rawPath = event.rawPath || event.requestContext?.http?.path;

  if (method === "POST" && rawPath === "/zen/v1/responses") return handleZen(event, responder, deps);
  const route = parseRoute(method, rawPath);
  if (!route) { emit("gate.not_found", env); return responder.error(notFound()); }

  let hold = null;
  let settled = false;
  try {
    const { ownerId, aipTiers } = await authenticate(headers, ddb, env);
    const tier = tierForModel(route.modelId, aipTiers); // 403 if not the owner's AIP (blocks Titan/embeddings too)

    let body;
    try { body = JSON.parse(eventBody(event).toString("utf8") || "{}"); }
    catch { throw forbiddenOp(); }
    clampMaxTokens(body, route.op, MODEL_MAX_TOKENS[tier] ?? 131072);
    const bodyBuf = Buffer.from(JSON.stringify(body), "utf8");

    // Resolve plan + budget rows (billing always; fast-gate only for paid).
    const billing = (await ddb.getItem({ TableName: env.BILLING_TABLE, Key: { owner_id: { S: ownerId } }, ConsistentRead: true }))?.Item;
    if (readS(billing?.plan) === "entry" || readS(billing?.provider_choice) === "opencode_zen" ||
        (env.MUSE_ENABLED === "true" && readS(billing?.subscription_status) === "trial_local")) throw forbiddenOp();
    const plan = resolvePlan(billing);
    const fastGateRow = plan === "paid"
      ? (await ddb.getItem({ TableName: env.FAST_GATE_TABLE, Key: { owner_id: { S: ownerId } } }))?.Item
      : null;

    const today = new Date(now).toISOString().slice(0, 10);
    ({ hold } = await reserveOrThrow({ ddb, env, ownerId, plan, billingRow: billing, fastGateRow, today, nowSec: Math.floor(now / 1000) }));
    emit("gate.allowed", env, { plan });

    // Forward verbatim. Path preserves the URL-encoded modelId exactly as received.
    const path = `/model/${route.rawModelId}/${route.op}`;
    const result = await forwardImpl({
      region: env.AWS_REGION, path, bodyBuf,
      writeHead: (s, h) => responder.writeHead(s, h),
      write: (c) => responder.write(c),
      end: () => responder.end(),
      abortSignal: responder.abortSignal,
    });

    if (result.status === 200) {
      const outcome = await settle({ ddb, env, hold, tier, requestId: result.requestId, usage: result.usage });
      settled = true;
      emit(outcome === "counted" ? "gate.debit_ok" : outcome === "dup" ? "gate.debit_skipped_dup" : "gate.client_abort", env, { plan });
    } else {
      emit("gate.upstream_error", env, { status: result.status });
      // hold released in finally (no spend on a non-200)
    }
  } catch (err) {
    if (err instanceof GateError) {
      emit(`gate.${err.code || "error"}`, env);
      if (!responder.headersSent()) responder.error(err);
      else responder.end();
    } else {
      emit("gate.error", env, { message: String(err?.message || err) });
      if (!responder.headersSent()) responder.error(internal());
      else responder.end();
    }
  } finally {
    if (hold && !settled) {
      try { await release({ ddb, env, hold }); }
      catch (e) { emit("gate.hold_release_failed", env, { message: String(e?.message || e) }); }
    }
  }
}
