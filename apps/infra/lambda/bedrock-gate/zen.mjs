// Included Muse uses the owner gate token, never the Bedrock credit ledger.
import { authenticate } from './auth.mjs';
import { readS, readN } from './ddb.mjs';
import { GateError } from './errors.mjs';
import { emit } from './metrics.mjs';

const error = (status, message) => new GateError(status, 'invalid_request_error', message, 'muse_denied');
const unavailable = () => error(503, 'Included Muse is not available');
const forbidden = () => error(403, 'Included Muse is not enabled for this workspace or its trial has ended');

async function entitlement(event, deps) {
  const {ownerId} = await authenticate(event.headers, deps.ddb, deps.env, {bedrock:false});
  const row = (await deps.ddb.getItem({TableName:deps.env.BILLING_TABLE, Key:{owner_id:{S:ownerId}}, ConsistentRead:true}))?.Item;
  const status = readS(row?.subscription_status);
  if (status === 'trial_local') {
    const end = readN(row?.trial_end);
    if (!Number.isFinite(end) || Date.now() >= end * 1000) throw forbidden();
    return {ownerId, deadline:end * 1000};
  }
  if (!['active','trialing'].includes(status) ||
      (readS(row?.plan) !== 'entry' && readS(row?.provider_choice) !== 'opencode_zen')) throw forbidden();
  return {ownerId, deadline:Infinity};
}

export async function handleZen(event, responder, deps) {
  const controller = new AbortController();
  let deadlineTimer, pollTimer;
  let complete = false;
  try {
    if (deps.env.MUSE_ENABLED !== 'true' || !deps.env.ZEN_SECRET_ID) throw unavailable();
    const {deadline} = await entitlement(event, deps);
    let body;
    try { body=JSON.parse(event.isBase64Encoded ? Buffer.from(event.body,'base64').toString('utf8') : event.body); }
    catch { throw error(400,'Invalid JSON'); }
    if (!body || typeof body !== 'object' || Array.isArray(body) || body.model !== 'muse-spark-1.3' ||
        body.previous_response_id != null || body.conversation != null || body.background === true ||
        (Array.isArray(body.input) && body.input.some(item => item?.type === 'item_reference'))) {
      throw error(400,'Only stateless Muse Responses requests are supported');
    }
    // Service output bound, not a claim about the vendor model's maximum.
    const outputLimit = Number(deps.env.MUSE_MAX_OUTPUT_TOKENS || 16384);
    if (!Number.isInteger(outputLimit) || outputLimit < 1) throw unavailable();
    body.max_output_tokens ??= outputLimit;
    if (!Number.isInteger(body.max_output_tokens) || body.max_output_tokens < 1 || body.max_output_tokens > outputLimit) {
      throw error(400,`max_output_tokens must be an integer from 1 to ${outputLimit}`);
    }
    body.store = false;
    deadlineTimer=setTimeout(()=>controller.abort(forbidden()),Math.max(1,Math.min(deadline-Date.now(),880000)));
    // ponytail: 15-second revocation polling; use a push signal if tighter cancellation is required.
    const recheck=async()=>{
      try { await entitlement(event,deps); }
      catch (err) { controller.abort(err); }
      if (!complete && !controller.signal.aborted) pollTimer=setTimeout(recheck,15000);
    };
    pollTimer=setTimeout(recheck,15000);
    const key=await deps.getZenKey();
    if (!key) throw unavailable();
    const upstream=await (deps.fetchImpl ?? fetch)('https://opencode.ai/zen/v1/responses',{
      method:'POST',redirect:'error',signal:controller.signal,
      headers:{'content-type':'application/json',authorization:`Bearer ${key}`},body:JSON.stringify(body),
    });
    if (upstream.status >= 300 && upstream.status < 400) throw error(502,'Unexpected upstream redirect');
    responder.writeHead(upstream.status,{'content-type':upstream.headers.get('content-type') || 'application/json',
      ...(upstream.headers.get('retry-after') ? {'retry-after':upstream.headers.get('retry-after')} : {})});
    if (upstream.body) for await (const chunk of upstream.body) responder.write(chunk);
    responder.end();
    emit('gate.muse_completed',deps.env,{status:upstream.status});
    // The stream is opaque: do not report an exact charge or hard dollar cap.
  } catch (err) {
    const failure=controller.signal.aborted ? controller.signal.reason : err;
    emit('gate.muse_failed',deps.env);
    if (responder.headersSent()) responder.end();
    else {
      const e=failure instanceof GateError ? failure : error(502,'Included Muse request failed');
      e.body=()=>JSON.stringify({error:{message:e.message,type:'invalid_request_error',code:e.code}});
      responder.error(e);
    }
  } finally { complete=true; clearTimeout(deadlineTimer);clearTimeout(pollTimer); }
}
