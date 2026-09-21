import { createHash, randomUUID } from 'node:crypto';
import { reserve, release, recordFree, releaseFree } from './ledger.mjs';
import { forward } from './forward.mjs';

class GateError extends Error {
  constructor(status, message) { super(message); this.status = status; }
  body() { return JSON.stringify({ error: { message: this.message, type: 'gate_error' } }); }
}
const fail = (status, message) => { throw new GateError(status, message); };
const S = (item, field) => item?.[field]?.S;
const routeFields = ['models', 'plugins', 'provider', 'route', 'user', 'metadata', 'trace', 'debug', 'service_tier', 'modalities', 'audio', 'prediction', 'stream_options'];
const allowedFields = new Set(['model', 'messages', 'stream', 'session_id', 'max_tokens', 'max_completion_tokens', 'temperature', 'top_p', 'stop', 'tools', 'tool_choice', 'parallel_tool_calls', 'response_format', 'reasoning', 'reasoning_effort', 'frequency_penalty', 'presence_penalty', 'seed']);

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
  for (const field of Object.keys(input)) if (routeFields.includes(field) || !allowedFields.has(field)) fail(400, 'Request option not allowed');
  for (const name of ['max_tokens', 'max_completion_tokens']) {
    const value = input[name];
    if (value !== undefined && (!Number.isSafeInteger(value) || value < 1 || value > Number(env.MAX_OUTPUT_TOKENS))) fail(400, 'Output limit exceeded');
  }
  if (!Number.isFinite(policy?.max_price?.prompt) || !Number.isFinite(policy?.max_price?.completion) || policy.max_price.prompt < 0 || policy.max_price.completion < 0) fail(503, 'Model policy unavailable');
  const { session_id: _ignoredSession, ...safeInput } = input;
  const body = { ...safeInput, provider: { max_price: policy.max_price, data_collection: 'deny' }, stream_options: { include_usage: true } };
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

export async function handleRequest(event, responder, { ddb, secrets, env, forwardImpl = forward }) {
  let ownerId, estimate, requestId;
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
    const trial = S(billing, 'subscription_status') === 'trial_local';
    const managed = S(billing, 'provider_choice') === 'openrouter_managed';
    if ((!managed && !trial) || (!trial && S(billing, 'subscription_status') !== 'active')) fail(403, 'Managed inference unavailable');
    const raw = event.body == null ? '' : event.isBase64Encoded ? Buffer.from(event.body, 'base64').toString('utf8') : event.body;
    if (!Number.isSafeInteger(Number(env.MAX_BODY_BYTES)) || Number(env.MAX_BODY_BYTES) < 1) fail(503, 'Request limit unavailable');
    if (Buffer.byteLength(raw) > Number(env.MAX_BODY_BYTES)) fail(413, 'Request too large');
    let input;
    try { input = JSON.parse(raw); } catch { fail(400, 'Invalid JSON'); }
    const policy = JSON.parse(env.MODEL_POLICY_JSON);
    if (trial && (policy.max_price?.prompt !== 0 || policy.max_price?.completion !== 0)) fail(403, 'Free model policy unavailable');
    const body = prepareBody(input, policy, ownerId, env);
    if (Buffer.byteLength(raw) > policy.max_prompt_tokens) fail(413, 'Prompt context bound exceeded');
    const keyRow = (await ddb.getItem({ TableName: env.OPENROUTER_KEYS_TABLE, Key: { owner_id: { S: ownerId } }, ConsistentRead: true }))?.Item;
    if (S(keyRow, 'state') !== 'active' || !S(keyRow, 'key_hash') || !S(keyRow, 'secret_name')) fail(503, 'Owner key unavailable');
    const secret = (await secrets.getSecretValue({ SecretId: S(keyRow, 'secret_name') })).SecretString;
    if (!secret || !secret.startsWith('sk-or-')) fail(503, 'Owner key unavailable');
    requestId = randomUUID();
    estimate = reservationFor(body, policy);
    const free = estimate === 0;
    if (!free && (!S(billing, 'metronome_customer_id') || !S(billing, 'metronome_contract_id'))) fail(429, 'Credit ledger unavailable');
    const outbox = { request_id: { S: requestId }, owner_id: { S: ownerId }, key_hash: { S: S(keyRow, 'key_hash') }, secret_name: { S: S(keyRow, 'secret_name') }, model: { S: input.model }, policy_revision: { S: String(policy.revision) }, billing_mode: { S: free ? 'free' : 'paid' }, reservation_microcents: { N: String(estimate) }, state: { S: 'pending' }, created_at: { N: String(Math.floor(Date.now() / 1000)) } };
    const admitted = free ? await recordFree(ddb, env, outbox) : await reserve(ddb, env, ownerId, estimate, outbox);
    if (!admitted) fail(429, 'Credit balance exhausted');
    let persistedId;
    const onGenerationId = async id => {
      if (!id || id === persistedId) return;
      await ddb.updateItem({ TableName: env.REQUESTS_TABLE, Key: { request_id: { S: requestId } }, UpdateExpression: 'SET generation_id = :id', ConditionExpression: '#state = :pending', ExpressionAttributeNames: { '#state': 'state' }, ExpressionAttributeValues: { ':id': { S: id }, ':pending': { S: 'pending' } } });
      persistedId = id;
    };
    const result = await forwardImpl({ body, key: secret, writeHead: (status, headers) => responder.writeHead(status, { 'content-type': headers['content-type']?.includes('text/event-stream') ? 'text/event-stream' : 'application/json' }), write: chunk => responder.write(chunk), waitForDrain: () => { if (!responder.waitForDrain) throw new Error('Client drain unavailable'); return responder.waitForDrain(); }, onGenerationId, end: () => responder.end(), abortSignal: responder.abortSignal });
    if (result.generationId) await onGenerationId(result.generationId);
    if (result.preGeneration === true && !persistedId) {
      if (free) await releaseFree(ddb, env, requestId);
      else await release(ddb, env, ownerId, estimate, requestId);
    }
  } catch (error) {
    if (!responder.headersSent()) responder.error(error instanceof GateError ? error : new GateError(502, 'Inference gateway unavailable'));
    else responder.end();
  }
}
