import { createHash, randomUUID } from 'node:crypto';
import { reserve, release, recordFree, releaseFree } from './ledger.mjs';
import { forward } from './forward.mjs';

class GateError extends Error {
  constructor(status, message, code) { super(message); this.status = status; this.code = code; }
  body() { return JSON.stringify({ error: { message: this.message, type: 'gate_error', ...(this.code ? { code: this.code } : {}) } }); }
}
// Mirrors bedrock-gate/errors.mjs trialCap: the code the limit card keys on.
const dailyCap = () => new GateError(429, 'Lightbulb: daily limit reached — resets at midnight UTC', 'trial_daily_cap');
const fail = (status, message) => { throw new GateError(status, message); };
const S = (item, field) => item?.[field]?.S;
const routeFields = ['models', 'plugins', 'provider', 'route', 'user', 'metadata', 'trace', 'debug', 'service_tier', 'modalities', 'audio', 'prediction'];
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

function reservationFor(body, policy) {
  const output = Math.max(body.max_tokens ?? 0, body.max_completion_tokens ?? 0);
  const amount = ceilPricedTokens(policy.max_prompt_tokens, policy.max_price.prompt) + ceilPricedTokens(output, policy.max_price.completion);
  if (amount < 0n || amount > BigInt(Number.MAX_SAFE_INTEGER)) fail(503, 'Model reservation unavailable');
  return Number(amount);
}

export function prepareBody(input, policy, ownerId, env) {
  if (!Number.isSafeInteger(Number(env.MAX_OUTPUT_TOKENS)) || Number(env.MAX_OUTPUT_TOKENS) < 1) fail(503, 'Request limit unavailable');
  if (!input || Array.isArray(input) || typeof input !== 'object') fail(400, 'Invalid request');
  if (!Array.isArray(policy.models) || !policy.models.includes(input.model)) fail(400, 'Model not allowed');
  if (input.stream !== true || !Array.isArray(input.messages) || !input.messages.length) fail(400, 'Streaming chat messages required');
  if (!Number.isSafeInteger(policy.max_prompt_tokens) || policy.max_prompt_tokens < 1) fail(503, 'Prompt context bound unavailable');
  for (const message of input.messages) {
    const content = message?.content;
    if (typeof content === 'string') continue;
    if (content === null && message?.role === 'assistant' && Array.isArray(message.tool_calls)) continue;
    if (!Array.isArray(content) || !content.every(part => part?.type === 'text' && typeof part.text === 'string' && Object.keys(part).every(key => key === 'type' || key === 'text'))) fail(400, 'Multimodal input unavailable');
  }
  if (input.tools !== undefined && (!Array.isArray(input.tools) || !input.tools.every(tool => tool?.type === 'function' && typeof tool.function?.name === 'string' && Object.keys(tool).every(key => key === 'type' || key === 'function')))) fail(400, 'Server tools unavailable');
  const rejected = Object.keys(input).filter(field => routeFields.includes(field) || !allowedFields.has(field));
  if (rejected.length) fail(400, `Request option not allowed: ${rejected.join(',')}`);
  for (const name of ['max_tokens', 'max_completion_tokens']) {
    const value = input[name];
    if (value !== undefined && (!Number.isSafeInteger(value) || value < 1 || value > Number(env.MAX_OUTPUT_TOKENS))) fail(400, 'Output limit exceeded');
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
  const first = input.messages.find(m => m?.role === 'user') ?? input.messages[0];
  body.session_id = createHash('sha256').update(ownerId).update('\0').update(JSON.stringify(first)).digest('hex');
  return body;
}

export async function handleRequest(event, responder, { ddb, secrets, env, forwardImpl = forward, log = console.log }) {
  let ownerId, estimate, requestId, model;
  try {
    if (env.OPENROUTER_MANAGED_ENABLED !== 'true') fail(503, 'Managed inference unavailable');
    if ((event.requestContext?.http?.method ?? event.httpMethod) !== 'POST' || event.rawPath !== '/v1/chat/completions') fail(404, 'Unknown route');
    const bearer = /^(?:Bearer)\s+(\S+)$/i.exec(event.headers?.authorization ?? event.headers?.Authorization ?? '')?.[1];
    if (!bearer) fail(401, 'Invalid gateway token');
    const hash = createHash('sha256').update(bearer).digest('hex');
    const token = (await ddb.getItem({ TableName: env.TOKENS_TABLE, Key: { token_hash: { S: hash } }, ConsistentRead: true }))?.Item;
    ownerId = S(token, 'owner_id');
    if (!ownerId || token?.denied?.BOOL) fail(token?.denied?.BOOL ? 403 : 401, 'Invalid gateway token');
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
    const secret = (await secrets.getSecretValue({ SecretId: S(keyRow, 'secret_name') })).SecretString;
    if (!secret || !secret.startsWith('sk-or-')) fail(503, 'Owner key unavailable');
    requestId = randomUUID();
    estimate = billingMode === 'paid' ? reservationFor(body, policy) : 0;
    if (billingMode === 'paid' && (!S(billing, 'metronome_customer_id') || !S(billing, 'metronome_contract_id'))) fail(429, 'Credit ledger unavailable');
    const outbox = { request_id: { S: requestId }, owner_id: { S: ownerId }, key_hash: { S: S(keyRow, 'key_hash') }, secret_name: { S: S(keyRow, 'secret_name') }, model: { S: input.model }, policy_revision: { S: String(policy.revision) }, billing_mode: { S: billingMode }, reservation_microcents: { N: String(estimate) }, state: { S: 'pending' }, created_at: { N: String(Math.floor(Date.now() / 1000)) } };
    const admitted = billingMode === 'paid' ? await reserve(ddb, env, ownerId, estimate, outbox) : await recordFree(ddb, env, outbox);
    if (!admitted) fail(429, 'Credit balance exhausted');
    // Owner question: do paying customers draw on the cheap-model budget?
    const tier = trial ? 'trial' : base ? 'base' : paidDefault ? 'paid_default' : 'managed_paid';
    log(JSON.stringify({ _aws: { Timestamp: Date.now(), CloudWatchMetrics: [{ Namespace: 'LightbulbAI', Dimensions: [['env', 'tier', 'billing_mode']], Metrics: [{ Name: 'openrouter.gate.request', Unit: 'Count' }] }] }, env: String(env.GATE_ENV), tier, billing_mode: billingMode, 'openrouter.gate.request': 1 }));
    let persistedId;
    const onGenerationId = async id => {
      if (!id || id === persistedId) return;
      await ddb.updateItem({ TableName: env.REQUESTS_TABLE, Key: { request_id: { S: requestId } }, UpdateExpression: 'SET generation_id = :id', ConditionExpression: '#state = :pending', ExpressionAttributeNames: { '#state': 'state' }, ExpressionAttributeValues: { ':id': { S: id }, ':pending': { S: 'pending' } } });
      persistedId = id;
    };
    // A capped owner's 402 is OpenRouter's per-key daily limit: answer with the
    // daily-cap 429 the limit card renders instead of the upstream body.
    let capHit = false;
    const result = await forwardImpl({ body, key: secret,
      writeHead: (upstreamStatus, headers) => {
        if (capped && upstreamStatus === 402) { capHit = true; return responder.writeHead(429, { 'content-type': 'application/json' }); }
        return responder.writeHead(upstreamStatus, { 'content-type': headers['content-type']?.includes('text/event-stream') ? 'text/event-stream' : 'application/json' });
      },
      write: chunk => capHit || responder.write(chunk),
      waitForDrain: () => { if (!responder.waitForDrain) throw new Error('Client drain unavailable'); return responder.waitForDrain(); }, onGenerationId,
      end: () => { if (capHit) responder.write(dailyCap().body()); responder.end(); },
      abortSignal: responder.abortSignal });
    if (result.generationId) await onGenerationId(result.generationId);
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
    log(JSON.stringify({ status: gateError.status, code: gateError.code ?? null, message: gateError.message, owner_id: ownerId ?? null, model: model ?? null, request_id: requestId ?? null, cause: error instanceof GateError ? null : String(error?.name ?? 'Error') }));
    if (!responder.headersSent()) responder.error(gateError);
    else responder.end();
  }
}
