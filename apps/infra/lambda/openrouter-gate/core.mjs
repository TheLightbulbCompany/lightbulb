import { createHash, randomUUID } from 'node:crypto';
import { reserve, release, recordFree, releaseFree } from './ledger.mjs';
import { forward } from './forward.mjs';

class GateError extends Error {
  constructor(status, message, code, extra) { super(message); this.status = status; this.code = code; this.extra = extra; }
  body() { return JSON.stringify({ error: { message: this.message, type: 'gate_error', ...(this.code ? { code: this.code } : {}), ...this.extra } }); }
}
// Mirrors bedrock-gate/errors.mjs trialCap: the code the limit card keys on.
const dailyCap = () => new GateError(429, 'Lightbulb: daily limit reached — resets at midnight UTC', 'trial_daily_cap');
const fail = (status, message) => { throw new GateError(status, message); };
const S = (item, field) => item?.[field]?.S;
// OpenRouter answers "Key limit exceeded" both when the key's own daily limit is spent and when
// the ACCOUNT has no credits left (2026-10-13: a dry account capped every trial with $0 spent).
// Only the key's own numbers tell them apart, read with the same key.
const SPENT_BELOW_USD = 0.01;
export async function keyInfo(key) {
  const res = await fetch('https://openrouter.ai/api/v1/key', { headers: { authorization: `Bearer ${key}` }, signal: AbortSignal.timeout(3000) });
  if (!res.ok) throw new Error(`key info ${res.status}`);
  return (await res.json())?.data ?? {};
}
// null means unproven: a failed read or a key with no limit never stamps a whole day.
async function keyRemainingUsd(key, keyInfoImpl) {
  try {
    const remaining = (await keyInfoImpl(key))?.limit_remaining;
    return remaining == null || !Number.isFinite(Number(remaining)) ? null : Number(remaining);
  } catch { return null; }
}
// What this request could cost at the policy's ceiling (max_price is USD per million tokens).
// ponytail: prompt tokens guessed at 4 bytes each; send the real count if a request ever lands between.
function requestCeilingUsd(body, policy, promptBytes) {
  const output = Math.max(body.max_tokens ?? 0, body.max_completion_tokens ?? 0);
  return (output * policy.max_price.completion + (promptBytes / 4) * policy.max_price.prompt) / 1e6;
}
// OpenRouter's reason, for the log: named fields only (metadata.flagged_input can echo the prompt).
function refusalFields(status, upstreamBody) {
  let error;
  try { error = JSON.parse(upstreamBody)?.error; } catch { /* not JSON: the status still logs */ }
  const text = value => value == null ? null : String(value).replace(/sk-or-[\w-]+/g, '[key]').slice(0, 300);
  return { upstream_status: status, upstream_code: text(error?.code), limit_source: text(error?.metadata?.limit_source), scope: text(error?.metadata?.scope), reason: text(error?.metadata?.reason), upstream_message: text(error?.message) };
}

// Shared by chat and the models-list probe: same bearer -> owner_id -> denied check.
// Returns {ownerId, agentId}; agentId is null for a legacy owner-level bearer (a mid-roll runtime).
export async function authenticateOwner(event, ddb, env) {
  const bearer = /^(?:Bearer)\s+(\S+)$/i.exec(event.headers?.authorization ?? event.headers?.Authorization ?? '')?.[1];
  if (!bearer) fail(401, 'Invalid gateway token');
  const hash = createHash('sha256').update(bearer).digest('hex');
  const token = (await ddb.getItem({ TableName: env.TOKENS_TABLE, Key: { token_hash: { S: hash } }, ConsistentRead: true }))?.Item;
  const ownerId = S(token, 'owner_id');
  if (!ownerId || token?.denied?.BOOL) fail(token?.denied?.BOOL ? 403 : 401, 'Invalid gateway token');
  return { ownerId, agentId: S(token, 'agent_id') || null };
}
const routeFields = ['models', 'plugins', 'provider', 'route', 'user', 'metadata', 'trace', 'debug', 'service_tier', 'modalities', 'audio', 'prediction'];
const isTextPart = part => part?.type === 'text' && typeof part.text === 'string' && Object.keys(part).every(key => key === 'type' || key === 'text');
// Inline (data: URL) images only: a remote URL makes the provider fetch an unbounded payload.
const isInlineImagePart = part => part?.type === 'image_url' && Object.keys(part).every(key => key === 'type' || key === 'image_url')
  && part.image_url && typeof part.image_url === 'object' && Object.keys(part.image_url).every(key => key === 'url' || key === 'detail')
  && typeof part.image_url.url === 'string' && /^data:image\/(?:png|jpeg|webp|gif);base64,/.test(part.image_url.url);
const allowedFields = new Set(['model', 'messages', 'stream', 'stream_options', 'session_id', 'max_tokens', 'max_completion_tokens', 'temperature', 'top_p', 'stop', 'tools', 'tool_choice', 'parallel_tool_calls', 'response_format', 'reasoning', 'reasoning_effort', 'frequency_penalty', 'presence_penalty', 'seed']);

function ceilPricedTokens(tokens, price) {
  const match = /^(\d+)(?:\.(\d+))?(?:e([+-]?\d+))?$/i.exec(String(price));
  if (!match) fail(503, 'Model price ceiling unavailable');
  const scale = (match[2]?.length ?? 0) - Number(match[3] ?? 0);
  if (Math.abs(scale) > 12) fail(503, 'Model price ceiling unavailable');
  const numerator = BigInt(match[1] + (match[2] ?? '')) * BigInt(tokens);
  const divisor = scale > 0 ? 10n ** BigInt(scale) : 1n;
  const scaled = scale < 0 ? numerator * 10n ** BigInt(-scale) : numerator;
  return (scaled + divisor - 1n) / divisor;
}

// Provider-cost ceiling x OPENROUTER_MARKUP (openrouter-pricing.json), the same multiplier the
// Metronome rate and the settlement debit apply. A missing or malformed markup fails closed (503).
function reservationFor(body, policy, markup) {
  const output = Math.max(body.max_tokens ?? 0, body.max_completion_tokens ?? 0);
  const cost = ceilPricedTokens(policy.max_prompt_tokens, policy.max_price.prompt) + ceilPricedTokens(output, policy.max_price.completion);
  if (!(Number(markup) >= 1)) fail(503, 'Model reservation unavailable');
  const amount = ceilPricedTokens(cost, markup);
  if (amount < 0n || amount > BigInt(Number.MAX_SAFE_INTEGER)) fail(503, 'Model reservation unavailable');
  return Number(amount);
}

export function prepareBody(input, policy, ownerId, env) {
  if (!Number.isSafeInteger(Number(env.MAX_OUTPUT_TOKENS)) || Number(env.MAX_OUTPUT_TOKENS) < 1) fail(503, 'Request limit unavailable');
  if (!input || Array.isArray(input) || typeof input !== 'object') fail(400, 'Invalid request');
  if (!Array.isArray(policy.models) || !policy.models.includes(input.model)) fail(400, 'Model not allowed');
  if (input.stream !== true || !Array.isArray(input.messages) || !input.messages.length) fail(400, 'Streaming chat messages required');
  if (!Number.isSafeInteger(policy.max_prompt_tokens) || policy.max_prompt_tokens < 1) fail(503, 'Prompt context bound unavailable');
  let hasImage = false;
  for (const message of input.messages) {
    const content = message?.content;
    if (typeof content === 'string') continue;
    if (content === null && message?.role === 'assistant' && Array.isArray(message.tool_calls)) continue;
    if (!Array.isArray(content) || !content.every(part => isTextPart(part) || (isInlineImagePart(part) && (hasImage = true)))) fail(400, 'Multimodal input unavailable');
  }
  // Images go to the policy's vision model: the chat model may be text-only, and Hermes's
  // unset auxiliary.vision sends its vision calls here under the chat model's id. The same
  // provider.max_price ceiling applies, so the reservation below still covers the call.
  if (hasImage && (typeof policy.vision_model !== 'string' || !policy.vision_model)) fail(400, 'Multimodal input unavailable');
  if (input.tools !== undefined && (!Array.isArray(input.tools) || !input.tools.every(tool => tool?.type === 'function' && typeof tool.function?.name === 'string' && Object.keys(tool).every(key => key === 'type' || key === 'function')))) fail(400, 'Server tools unavailable');
  const rejected = Object.keys(input).filter(field => routeFields.includes(field) || !allowedFields.has(field));
  if (rejected.length) fail(400, `Request option not allowed: ${rejected.join(',')}`);
  // An output ceiling above ours is clamped, not refused: Hermes picks max_tokens from the model's
  // own context, and a 400 here turned into "The AI model is having problems" for the person (24
  // turns across 11 workspaces, 2026-10-01..08). The reservation below prices the clamped value.
  for (const name of ['max_tokens', 'max_completion_tokens']) {
    const value = input[name];
    if (value !== undefined && (!Number.isSafeInteger(value) || value < 1)) fail(400, 'Output limit exceeded');
    if (value !== undefined && value > Number(env.MAX_OUTPUT_TOKENS)) input = { ...input, [name]: Number(env.MAX_OUTPUT_TOKENS) };
  }
  if (!Number.isFinite(policy?.max_price?.prompt) || !Number.isFinite(policy?.max_price?.completion) || policy.max_price.prompt < 0 || policy.max_price.completion < 0) fail(503, 'Model policy unavailable');
  const { session_id: _ignoredSession, ...safeInput } = input;
  // Free models are training-subsidized: no endpoint serves them under a deny
  // policy, so a zero-price policy allows training while any priced policy
  // keeps deny. Paid inference stays deny-only.
  const freePolicy = policy.max_price?.prompt === 0 && policy.max_price?.completion === 0;
  const routing = policy.provider ?? {};
  const strings = value => Array.isArray(value) && value.every(item => typeof item === 'string');
  if (typeof routing !== 'object' || Array.isArray(routing) || Object.keys(routing).some(key => !['order', 'ignore', 'allow_fallbacks'].includes(key))
    || (routing.order !== undefined && !strings(routing.order)) || (routing.ignore !== undefined && !strings(routing.ignore))
    || (routing.allow_fallbacks !== undefined && typeof routing.allow_fallbacks !== 'boolean')) fail(503, 'Model policy unavailable');
  const body = { ...safeInput, provider: { max_price: policy.max_price, data_collection: freePolicy ? 'allow' : 'deny', ...routing }, stream_options: { include_usage: true } };
  if (body.max_tokens === undefined && body.max_completion_tokens === undefined) body.max_tokens = Number(env.MAX_OUTPUT_TOKENS);
  if (input.model === 'openrouter/auto') {
    if (!Array.isArray(policy.auto?.allowed_models) || !policy.auto.allowed_models.length || !policy.auto.allowed_models.every(m => policy.models.includes(m) && m !== 'openrouter/auto')) fail(503, 'Auto policy unavailable');
    if (!['low', 'medium', 'high', 'xhigh', 'max'].includes(policy.auto.cost_tier)) fail(503, 'Auto policy unavailable');
    body.plugins = [{ id: 'auto-router', allowed_models: policy.auto.allowed_models, cost_tier: policy.auto.cost_tier }];
  }
  if (hasImage) { body.model = policy.vision_model; delete body.plugins; }
  const first = input.messages.find(m => m?.role === 'user') ?? input.messages[0];
  body.session_id = createHash('sha256').update(ownerId).update('\0').update(JSON.stringify(first)).digest('hex');
  return body;
}

export async function handleRequest(event, responder, { ddb, secrets, env, forwardImpl = forward, keyInfoImpl = keyInfo, log = console.log }) {
  let ownerId, agentId, estimate, requestId, model;
  try {
    const method = event.requestContext?.http?.method ?? event.httpMethod;
    // Hermes's OpenAI-compatible "custom" provider resolves context length for a base_url it
    // doesn't recognize by probing an OpenAI-shaped models list, trying "<base_url>/models" and,
    // when base_url already ends in "/v1", the stripped "<host>/models" too (agent/model_metadata.py
    // fetch_endpoint_model_metadata) -- both land here as pre-auth 404s. Answer them instead.
    if (method === 'GET' && (event.rawPath === '/v1/models' || event.rawPath === '/models')) {
      ({ ownerId, agentId } = await authenticateOwner(event, ddb, env));
      const policy = JSON.parse(env.MODEL_POLICY_JSON);
      const data = (policy.models ?? []).map(id => ({ id, object: 'model', owned_by: 'lightbulb' }));
      responder.writeHead(200, { 'content-type': 'application/json' });
      responder.write(JSON.stringify({ object: 'list', data }));
      responder.end();
      return;
    }
    if (method !== 'POST' || event.rawPath !== '/v1/chat/completions') fail(404, 'Unknown route');
    ({ ownerId, agentId } = await authenticateOwner(event, ddb, env));
    const billing = (await ddb.getItem({ TableName: env.BILLING_TABLE, Key: { owner_id: { S: ownerId } }, ConsistentRead: true }))?.Item;
    const status = S(billing, 'subscription_status');
    // Capped traffic costs real money now: an EXPIRED trial and a lapsed Base are refused
    // here, not only by the backend's pre-send gates (a warm runtime can still call us).
    const trialEnd = Number(billing?.trial_end?.N);
    const trial = status === 'trial_local' && !(Number.isFinite(trialEnd) && Date.now() / 1000 > trialEnd);
    const base = S(billing, 'plan') === 'base' && ['active', 'trialing'].includes(status);
    // Interim default: a paid Workspace/Ultra row that never chose a provider is
    // treated exactly like Base. Lockstep with trial_cap.is_managed_capped.
    const paidDefault = ['standard', 'ultra_pro'].includes(S(billing, 'plan')) && !S(billing, 'provider_choice') && ['active', 'trialing'].includes(status);
    // Capped owners run on OpenRouter's per-key daily $ limit, never Metronome.
    const capped = trial || base || paidDefault;
    const managed = S(billing, 'provider_choice') === 'openrouter_managed';
    if (!capped && !(managed && status === 'active')) fail(403, 'Managed inference unavailable');
    const today = new Date().toISOString().slice(0, 10);
    if (capped && S(billing, 'openrouter_capped_day') === today) throw dailyCap();
    const raw = event.body == null ? '' : event.isBase64Encoded ? Buffer.from(event.body, 'base64').toString('utf8') : event.body;
    if (!Number.isSafeInteger(Number(env.MAX_BODY_BYTES)) || Number(env.MAX_BODY_BYTES) < 1) fail(503, 'Request limit unavailable');
    if (Buffer.byteLength(raw) > Number(env.MAX_BODY_BYTES)) fail(413, 'Request too large');
    let input;
    try { input = JSON.parse(raw); } catch { fail(400, 'Invalid JSON'); }
    model = String(input?.model ?? '').slice(0, 120) || null;
    const policy = JSON.parse(env.MODEL_POLICY_JSON);
    const body = prepareBody(input, policy, ownerId, env);
    const free = policy.max_price?.prompt === 0 && policy.max_price?.completion === 0;
    const billingMode = free ? 'free' : capped ? 'capped' : 'paid';
    // A token spans >= 1 byte, so bytes <= max_prompt_tokens proves the hold (priced at
    // max_prompt_tokens) covers the prompt. Only a Metronome hold needs that proof; free
    // and capped requests hold nothing, so MAX_BODY_BYTES alone bounds them (a Hermes
    // first turn is >100 KB of JSON).
    if (billingMode === 'paid' && Buffer.byteLength(raw) > policy.max_prompt_tokens) fail(413, 'Prompt context bound exceeded');
    const keyRow = (await ddb.getItem({ TableName: env.OPENROUTER_KEYS_TABLE, Key: { owner_id: { S: ownerId } }, ConsistentRead: true }))?.Item;
    if (S(keyRow, 'state') !== 'active' || !S(keyRow, 'key_hash') || !S(keyRow, 'secret_name')) fail(503, 'Owner key unavailable');
    const secret = await secrets.ownerKey(ownerId, S(keyRow, 'key_hash'), S(keyRow, 'secret_name'));
    if (!secret || !secret.startsWith('sk-or-')) fail(503, 'Owner key unavailable');
    requestId = randomUUID();
    estimate = billingMode === 'paid' ? reservationFor(body, policy, env.OPENROUTER_MARKUP) : 0;
    if (billingMode === 'paid' && (!S(billing, 'metronome_customer_id') || !S(billing, 'metronome_contract_id'))) fail(429, 'Credit ledger unavailable');
    const outbox = { request_id: { S: requestId }, owner_id: { S: ownerId }, ...(agentId ? { agent_id: { S: agentId } } : {}), key_hash: { S: S(keyRow, 'key_hash') }, secret_name: { S: S(keyRow, 'secret_name') }, model: { S: body.model }, policy_revision: { S: String(policy.revision) }, billing_mode: { S: billingMode }, reservation_microcents: { N: String(estimate) }, state: { S: 'pending' }, created_at: { N: String(Math.floor(Date.now() / 1000)) } };
    const admitted = billingMode === 'paid' ? await reserve(ddb, env, ownerId, estimate, outbox) : await recordFree(ddb, env, outbox);
    if (!admitted) fail(429, 'Credit balance exhausted');
    // Owner question: do paying customers draw on the cheap-model budget?
    const tier = trial ? 'trial' : base ? 'base' : paidDefault ? 'paid_default' : 'managed_paid';
    // model is what goes upstream (after the vision reroute); image says whether one rode along.
    // Properties, not dimensions: they are for log search, not metric cardinality.
    const image = input.messages.some(m => Array.isArray(m?.content) && m.content.some(part => part?.type === 'image_url'));
    log(JSON.stringify({ _aws: { Timestamp: Date.now(), CloudWatchMetrics: [{ Namespace: 'LightbulbAI', Dimensions: [['env', 'tier', 'billing_mode']], Metrics: [{ Name: 'openrouter.gate.request', Unit: 'Count' }] }] }, env: String(env.GATE_ENV), tier, billing_mode: billingMode, model: body.model, image, request_id: requestId, 'openrouter.gate.request': 1 }));
    let persistedId;
    const onGenerationId = async id => {
      if (!id || id === persistedId) return;
      await ddb.updateItem({ TableName: env.REQUESTS_TABLE, Key: { request_id: { S: requestId } }, UpdateExpression: 'SET generation_id = :id', ConditionExpression: '#state = :pending', ExpressionAttributeNames: { '#state': 'state' }, ExpressionAttributeValues: { ':id': { S: id }, ':pending': { S: 'pending' } } });
      persistedId = id;
    };
    // A capped owner's per-key daily limit: OpenRouter answers 403 "Key limit exceeded".
    // Answer with the daily-cap 429 the limit card renders. Every 403 is held until its body
    // says why: a disabled key must surface as itself, not a cap, and "Key limit exceeded" on a
    // key with money left (any owner) means the account is empty. A 402 is either: OpenRouter's docs
    // list limit_source openrouter_key_limit as well as openrouter_credits. Only the key's remaining
    // allowance tells them apart: too little for this request is the cap (2026-10-30, $0.004 left,
    // 503s for the rest of the day); real allowance left is the empty account and the paging 503.
    // Every capped 402 used to be taken as the cap, which stamped every trial for the day and never
    // paged (2026-10-26, $0.0005 left on the account).
    let capHit = false, held403 = null, held402 = false, keyLimit = false, refusal = '';
    const result = await forwardImpl({ body, key: secret,
      writeHead: (upstreamStatus, headers) => {
        if (capped && upstreamStatus === 402) { held403 = []; held402 = true; return; }
        if (upstreamStatus === 403) { held403 = []; return; }
        return responder.writeHead(upstreamStatus, { 'content-type': headers['content-type']?.includes('text/event-stream') ? 'text/event-stream' : 'application/json' });
      },
      write: chunk => capHit || (held403 ? held403.push(chunk) > 0 : responder.write(chunk)),
      waitForDrain: () => { if (!responder.waitForDrain) throw new Error('Client drain unavailable'); return responder.waitForDrain(); }, onGenerationId,
      end: (upstreamBody = '') => {
        if (held403 && (held402 || /key limit exceeded/i.test(upstreamBody))) { keyLimit = true; refusal = upstreamBody; return; } // answered below, once the key's spend is read
        else if (held403) { responder.writeHead(403, { 'content-type': 'application/json' }); held403.forEach(chunk => responder.write(chunk)); }
        if (capHit) responder.write(dailyCap().body());
        responder.end();
      },
      abortSignal: responder.abortSignal });
    if (keyLimit) {
      const remaining = await keyRemainingUsd(secret, keyInfoImpl);
      // Owner question: what did OpenRouter actually say when it refused?
      log(JSON.stringify({ ...refusalFields(held402 ? 402 : 403, refusal), limit_remaining: remaining, owner_id: ownerId, tier, request_id: requestId }));
      const spentBelow = capped ? Math.max(SPENT_BELOW_USD, requestCeilingUsd(body, policy, Buffer.byteLength(raw))) : SPENT_BELOW_USD;
      // OpenRouter names the 402's source: only openrouter_key_limit is the key's cap. Credits and the
      // in-flight budget are the account (the latter transient), so they stay the paging 503. Absent: the heuristic.
      const notTheKey = ['openrouter_credits', 'openrouter_in_flight_budget'].includes(refusalFields(0, refusal).limit_source);
      if (remaining !== null && !notTheKey && remaining < spentBelow) {
        if (capped) {
          capHit = true;
          responder.writeHead(429, { 'content-type': 'application/json' }); responder.write(dailyCap().body()); responder.end();
        } else { // a paid key's high backstop limit: passed through as it always was
          responder.writeHead(403, { 'content-type': 'application/json' }); held403.forEach(chunk => responder.write(chunk)); responder.end();
        }
      } else {
        // Owner question: is OpenRouter refusing keys that still have money (an empty account)?
        log(JSON.stringify({ _aws: { Timestamp: Date.now(), CloudWatchMetrics: [{ Namespace: 'LightbulbAI', Dimensions: [['env', 'service']], Metrics: [{ Name: 'openrouter.gate.account_refused', Unit: 'Count' }] }] }, env: String(env.GATE_ENV), service: 'openrouter-gate', tier, owner_id: ownerId, 'openrouter.gate.account_refused': 1 }));
        responder.writeHead(503, { 'content-type': 'application/json' }); responder.write(new GateError(503, 'Inference provider unavailable', 'provider_unavailable').body()); responder.end();
      }
    }
    if (result.generationId) await onGenerationId(result.generationId);
    // The model that actually answered (openrouter/auto picks one): kept on the ledger row so a downgrade is visible.
    if (result.model && result.generationId) {
      try { await ddb.updateItem({ TableName: env.REQUESTS_TABLE, Key: { request_id: { S: requestId } }, UpdateExpression: 'SET resolved_model = :model', ConditionExpression: 'attribute_exists(request_id)', ExpressionAttributeValues: { ':model': { S: String(result.model) } } }); } catch (error) { console.error('resolved model stamp failed', error?.name); }
    }
    // Real cost x markup, in the reservation's own unit (ceilPricedTokens; settlement's debit_microcents:
    // nanodollars x markup / 1000). Absent on a cut stream: settlement's /generation fallback fills it.
    if (result.cost != null && result.generationId) {
      try {
        const costMicrocents = (ceilPricedTokens(Math.round(result.cost * 1e9), env.OPENROUTER_MARKUP) + 999n) / 1000n;
        await ddb.updateItem({ TableName: env.REQUESTS_TABLE, Key: { request_id: { S: requestId } }, UpdateExpression: 'SET cost_microcents = :cost', ConditionExpression: 'attribute_exists(request_id)', ExpressionAttributeValues: { ':cost': { N: String(costMicrocents) } } });
      } catch (error) { console.error('cost stamp failed', error?.name); }
    }
    if (result.preGeneration === true && !persistedId) {
      if (billingMode === 'paid') await release(ddb, env, ownerId, estimate, requestId);
      else await releaseFree(ddb, env, requestId);
    }
    if (capHit && result.preGeneration === true) {
      try {
        await ddb.updateItem({ TableName: env.BILLING_TABLE, Key: { owner_id: { S: ownerId } }, UpdateExpression: 'SET openrouter_capped_day = :day', ConditionExpression: 'attribute_exists(owner_id)', ExpressionAttributeValues: { ':day': { S: today } } });
      } catch (error) { console.error('openrouter capped-day stamp failed', error?.name); }
      // Owner question: how often does each plan hit its daily cap?
      log(JSON.stringify({ _aws: { Timestamp: Date.now(), CloudWatchMetrics: [{ Namespace: 'LightbulbAI', Dimensions: [['env', 'tier']], Metrics: [{ Name: 'openrouter.gate.cap_hit', Unit: 'Count' }] }] }, env: String(env.GATE_ENV), tier, 'openrouter.gate.cap_hit': 1 }));
    }
  } catch (error) {
    const gateError = error instanceof GateError ? error : new GateError(502, 'Inference gateway unavailable');
    log(JSON.stringify({ status: gateError.status, code: gateError.code ?? null, message: gateError.message, owner_id: ownerId ?? null, agent_id: agentId ?? null, model: model ?? null, request_id: requestId ?? null, cause: error instanceof GateError ? null : String(error?.name ?? 'Error') }));
    if (!responder.headersSent()) responder.error(gateError);
    else responder.end();
  }
}
