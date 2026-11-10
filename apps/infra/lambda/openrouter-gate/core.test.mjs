import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { handleRequest, prepareBody, authenticateOwner } from './core.mjs';

const policy = { revision: 'test-1', models: ['anthropic/claude-sonnet-4.5', 'openrouter/auto'], auto: { allowed_models: ['anthropic/claude-sonnet-4.5'], cost_tier: 'low' }, max_price: { prompt: 0.5, completion: 2 }, max_prompt_tokens: 100000 };
const env = { TOKENS_TABLE: 'tokens', BILLING_TABLE: 'billing', FAST_GATE_TABLE: 'fast', OPENROUTER_KEYS_TABLE: 'keys', REQUESTS_TABLE: 'requests', RESERVE_ESTIMATE_MICROCENTS: '300000', RESERVE_TTL_SECONDS: '1200', MODEL_POLICY_JSON: JSON.stringify(policy), MAX_BODY_BYTES: '100000', MAX_OUTPUT_TOKENS: '8192', OPENROUTER_MARKUP: '1.2' };
const AV = (s) => ({ S: s });

function fixture({ token = 'good', status = 'active', balance = 1000000, provider = 'openrouter_managed', plan, keyState = 'active', cappedDay, metronome = true, trialEnd, voice = {} } = {}) {
  const calls = { forwards: [], puts: [], updates: [], errors: [], chunks: [], logs: [] };
  const ddb = {
    async getItem(p) {
      if (p.TableName === 'tokens') return { Item: token === 'good' ? { owner_id: AV('owner-1') } : undefined };
      if (p.TableName === 'billing') return { Item: { ...(provider ? { provider_choice: AV(provider) } : {}), ...(plan ? { plan: AV(plan) } : {}), ...(cappedDay ? { openrouter_capped_day: AV(cappedDay) } : {}), ...(trialEnd !== undefined ? { trial_end: { N: String(trialEnd) } } : {}), subscription_status: AV(status), ...(metronome ? { metronome_customer_id: AV('mc'), metronome_contract_id: AV('ct') } : {}), ...voice } };
      if (p.TableName === 'fast') return { Item: balance == null ? undefined : { remaining_microcents: { N: String(balance) }, reserved_microcents: { N: '0' } } };
      if (p.TableName === 'keys') return { Item: { state: AV(keyState), key_hash: AV('hash-1'), secret_name: AV('secret-1') } };
    },
    async updateItem(p) { calls.updates.push(p); },
    async putItem(p) { calls.puts.push(p); },
    async transactWriteItems(p) { for (const operation of p.TransactItems) { if (operation.Put) calls.puts.push(operation.Put); if (operation.Update) calls.updates.push(operation.Update); } },
  };
  const responder = {
    headersSent: () => !!calls.head,
    writeHead(status, headers) { calls.head = { status, headers }; },
    write(chunk) { calls.chunks.push(Buffer.from(chunk)); },
    end() { calls.ended = true; },
    error(err) { calls.errors.push(err); },
  };
  const deps = { ddb, env, log: line => calls.logs.push(JSON.parse(line)), secrets: { async ownerKey() { return 'sk-or-secret'; } }, forwardImpl: async (p) => { calls.forwards.push(p); p.writeHead(200, { 'content-type': 'text/event-stream' }); p.write(Buffer.from('data: {"id":"gen-1","choices":[]}\n\n')); p.end(); return { status: 200, generationId: 'gen-1' }; } };
  const event = (body = { model: policy.models[0], messages: [{ role: 'user', content: 'Hi' }], stream: true }, headers = {}) => ({ rawPath: '/v1/chat/completions', requestContext: { http: { method: 'POST' } }, headers: { authorization: `Bearer ${token}`, ...headers }, body: JSON.stringify(body) });
  return { calls, deps, responder, event };
}

const collectedVoice = { metronome_voice_contract_id: AV('voice'), voice_plan_stripe_status: AV('active') };
test('fresh collected voice works after local trial expiry on included key budget without a credits ledger', async () => {
  const f = fixture({ status: 'trial_local', trialEnd: 1, provider: null, balance: null, metronome: false, voice: collectedVoice });
  await handleRequest(f.event(), f.responder, f.deps);
  assert.equal(f.calls.errors.length, 0);
  assert.equal(f.calls.forwards.length, 1);
  assert.equal(f.calls.puts[0].Item.billing_mode.S, 'capped');
  assert.equal(f.calls.puts[0].Item.reservation_microcents.N, '0');
});
test('failed or ended voice cannot revive an expired local trial at the money fence', async () => {
  for (const extra of [{ voice_plan_stripe_status: AV('past_due') }, { voice_plan_ending_at: AV('2000-01-01T00:00:00Z') }]) {
    const f = fixture({ status: 'trial_local', trialEnd: 1, provider: null, voice: { ...collectedVoice, ...extra } });
    await handleRequest(f.event(), f.responder, f.deps);
    assert.equal(f.calls.errors[0].status, 403);
    assert.equal(f.calls.forwards.length, 0);
  }
});
test('explicit premium managed choice on voice still needs owner-funded credits', async () => {
  const f = fixture({ status: 'trial_local', trialEnd: 1, provider: 'openrouter_managed', metronome: false, voice: collectedVoice });
  await handleRequest(f.event(), f.responder, f.deps);
  assert.equal(f.calls.errors[0].status, 429);
  assert.equal(f.calls.forwards.length, 0);
});

test('authenticateOwner resolves a per-agent bearer to {ownerId, agentId}; a legacy owner bearer gets agentId null; unknown 401s; denied 403s', async () => {
  const hashOf = (t) => createHash('sha256').update(t).digest('hex');
  const byHash = {
    [hashOf('agent')]: { owner_id: AV('owner-1'), agent_id: AV('agent-7') },
    [hashOf('owner')]: { owner_id: AV('owner-1') },
    [hashOf('denied')]: { owner_id: AV('owner-1'), agent_id: AV('agent-7'), denied: { BOOL: true } },
  };
  const ddb = { async getItem(p) { return { Item: byHash[p.Key.token_hash.S] }; } };
  const ev = (t) => ({ headers: { authorization: `Bearer ${t}` } });
  assert.deepEqual(await authenticateOwner(ev('agent'), ddb, env), { ownerId: 'owner-1', agentId: 'agent-7' });
  assert.deepEqual(await authenticateOwner(ev('owner'), ddb, env), { ownerId: 'owner-1', agentId: null });
  await assert.rejects(authenticateOwner(ev('forged'), ddb, env), { status: 401 });
  await assert.rejects(authenticateOwner(ev('denied'), ddb, env), { status: 403 });
});
test('rejects unknown bearer before billing or upstream', async () => { const f = fixture({ token: 'wrong' }); await handleRequest(f.event(), f.responder, f.deps); assert.equal(f.calls.errors[0].status, 401); assert.equal(f.calls.forwards.length, 0); });
test('zero-price model records a free outbox without a Metronome reservation', async () => { const f = fixture({ balance: null }); f.deps.env = { ...env, MODEL_POLICY_JSON: JSON.stringify({ ...policy, models: ['nvidia/nemotron-3-ultra-550b-a55b:free'], max_price: { prompt: 0, completion: 0 } }) }; await handleRequest(f.event({ model: 'nvidia/nemotron-3-ultra-550b-a55b:free', messages: [{ role: 'user', content: 'Hi' }], stream: true }), f.responder, f.deps); assert.equal(f.calls.errors.length, 0, f.calls.errors[0]?.message); assert.equal(f.calls.forwards.length, 1); assert.equal(f.calls.puts.length, 1); assert.equal(f.calls.puts[0].Item.reservation_microcents.N, '0'); assert.equal(f.calls.puts[0].Item.billing_mode.S, 'free'); });
test('trial_local owner may use only the zero-price policy without a provider choice or Metronome contract', async () => { const f = fixture({ balance: null, status: 'trial_local', provider: 'none' }); f.deps.env = { ...env, MODEL_POLICY_JSON: JSON.stringify({ ...policy, models: ['nvidia/nemotron-3-ultra-550b-a55b:free'], max_price: { prompt: 0, completion: 0 } }) }; await handleRequest(f.event({ model: 'nvidia/nemotron-3-ultra-550b-a55b:free', messages: [{ role: 'user', content: 'Hi' }], stream: true }), f.responder, f.deps); assert.equal(f.calls.errors.length, 0, f.calls.errors[0]?.message); assert.equal(f.calls.forwards.length, 1); });
test('base owner may use the zero-price policy without a stored provider choice', async () => { const f = fixture({ balance: null, status: 'active', provider: 'none', plan: 'base' }); f.deps.env = { ...env, MODEL_POLICY_JSON: JSON.stringify({ ...policy, models: ['nvidia/nemotron-3-ultra-550b-a55b:free'], max_price: { prompt: 0, completion: 0 } }) }; await handleRequest(f.event({ model: 'nvidia/nemotron-3-ultra-550b-a55b:free', messages: [{ role: 'user', content: 'Hi' }], stream: true }), f.responder, f.deps); assert.equal(f.calls.errors.length, 0, f.calls.errors[0]?.message); assert.equal(f.calls.forwards.length, 1); });
test('rejects non-managed, canceled, and missing-balance owners', async () => { for (const options of [{ provider: 'byo_key' }, { status: 'canceled' }, { balance: null }]) { const f = fixture(options); await handleRequest(f.event(), f.responder, f.deps); assert.ok(f.calls.errors[0].status >= 400); assert.equal(f.calls.forwards.length, 0); } });
test('rejects unapproved model and client routing overrides', async () => { for (const body of [{ model: 'evil/model' }, { model: policy.models[0], models: ['evil/model'] }, { model: policy.models[0], plugins: [{ id: 'auto-router' }] }, { model: policy.models[0], provider: { max_price: { prompt: 100 } } }, { model: policy.models[0], stream: false }, { model: policy.models[0], max_tokens: 0 }, { model: policy.models[0], max_tokens: 1.5 }]) { const f = fixture(); await handleRequest(f.event(body), f.responder, f.deps); assert.equal(f.calls.errors[0].status, 400); assert.equal(f.calls.forwards.length, 0); } });
test('a refusal logs status, message, and the parsed model without leaking the bearer', async () => { const f = fixture(); await handleRequest(f.event({ model: 'evil/model', messages: [{ role: 'user', content: 'Hi' }], stream: true }), f.responder, f.deps); const refusal = f.calls.logs.find(entry => entry.status === 400); assert.equal(refusal.model, 'evil/model'); assert.equal(refusal.message, 'Model not allowed'); assert.equal(refusal.owner_id, 'owner-1'); assert.equal(refusal.request_id, null); assert.equal(JSON.stringify(refusal).includes('good'), false); });
test('reserves, persists pending request, forwards SSE, and keeps hold for reconciliation', async () => { const f = fixture(); await handleRequest(f.event(), f.responder, f.deps); assert.equal(f.calls.errors.length, 0); assert.equal(f.calls.forwards.length, 1); assert.equal(f.calls.puts.length, 1); assert.equal(f.calls.puts[0].Item.state.S, 'pending'); assert.equal(f.calls.puts[0].Item.key_hash.S, 'hash-1'); assert.equal(f.calls.head.status, 200); assert.equal(f.calls.chunks[0].toString().includes('gen-1'), true); assert.equal(JSON.stringify(f.calls.head).includes('sk-or-'), false); assert.equal(f.calls.updates.length, 2); });
test('outbox remembers the historical secret across key rotation', async () => { const f = fixture(); await handleRequest(f.event(), f.responder, f.deps); assert.equal(f.calls.puts[0].Item.secret_name.S, 'secret-1'); });
test('auto router receives server restrictions and owner-scoped session', () => { const body = prepareBody({ model: 'openrouter/auto', messages: [{ role: 'user', content: 'Hi' }], stream: true, session_id: 'client-id' }, policy, 'owner-1', env); assert.deepEqual(body.plugins, [{ id: 'auto-router', allowed_models: policy.auto.allowed_models, cost_tier: 'low' }]); assert.deepEqual(body.provider, { max_price: policy.max_price, data_collection: 'deny' }); assert.notEqual(body.session_id, 'client-id'); assert.equal(body.stream_options.include_usage, true); });
test('clamps an output ceiling above ours instead of refusing the turn', async () => { const f = fixture(); await handleRequest(f.event({ model: policy.models[0], messages: [{ role: 'user', content: 'Hi' }], stream: true, max_tokens: 1000000, max_completion_tokens: 9000 }), f.responder, f.deps); assert.equal(f.calls.errors.length, 0, f.calls.errors[0]?.message); assert.equal(f.calls.forwards.length, 1); const sent = f.calls.forwards[0].body; assert.equal(sent.max_tokens, 8192); assert.equal(sent.max_completion_tokens, 8192); });
test('fills an omitted output ceiling', () => { const body = prepareBody({ model: policy.models[0], messages: [{ role: 'user', content: 'Hi' }], stream: true }, policy, 'owner-1', env); assert.equal(body.max_tokens, 8192); });
test('zero-price policy allows training while priced policy keeps deny', () => { const free = { ...policy, models: ['nvidia/nemotron-3-ultra-550b-a55b:free'], max_price: { prompt: 0, completion: 0 } }; assert.equal(prepareBody({ model: 'nvidia/nemotron-3-ultra-550b-a55b:free', messages: [{ role: 'user', content: 'Hi' }], stream: true }, free, 'owner-1', env).provider.data_collection, 'allow'); assert.equal(prepareBody({ model: policy.models[0], messages: [{ role: 'user', content: 'Hi' }], stream: true }, policy, 'owner-1', env).provider.data_collection, 'deny'); });
test('upstream 429 does not disclose its response and releases reservation', async () => { const f = fixture(); f.deps.forwardImpl = async p => { p.writeHead(429, { 'content-type': 'application/json', 'x-secret': 'sk-or-secret' }); p.write(Buffer.from('sk-or-secret')); p.end(); return { status: 429, preGeneration: true }; }; await handleRequest(f.event(), f.responder, f.deps); assert.equal(f.calls.head.status, 429); assert.equal(JSON.stringify(f.calls.head).includes('sk-or-secret'), false); assert.equal(f.calls.updates.length, 3); });
test('stream disconnect remains pending for reconciliation', async () => { const f = fixture(); f.deps.forwardImpl = async p => { p.writeHead(200, { 'content-type': 'text/event-stream' }); p.write(Buffer.from('data: {"id":"gen-1"}\n\n')); throw new Error('client disconnected'); }; await handleRequest(f.event(), f.responder, f.deps); assert.equal(f.calls.puts[0].Item.state.S, 'pending'); assert.equal(f.calls.updates.length, 1); assert.equal(f.calls.ended, true); });
test('generation ID is durable before a disconnected client sees the first chunk', async () => { const f = fixture(); f.deps.forwardImpl = async p => { p.writeHead(200, { 'content-type': 'text/event-stream' }); await p.onGenerationId('gen-1'); assert.equal(f.calls.updates[1].ExpressionAttributeValues[':id'].S, 'gen-1'); p.write(Buffer.from('data: {"id":"gen-1"}\n\n')); throw new Error('client disconnected'); }; await handleRequest(f.event(), f.responder, f.deps); assert.equal(f.calls.puts[0].Item.state.S, 'pending'); assert.equal(f.calls.updates.length, 2); });
test('definite pre-generation 400 and 401 release the hold', async () => { for (const status of [400, 401]) { const f = fixture(); f.deps.forwardImpl = async p => { p.writeHead(status, { 'content-type': 'application/json' }); p.end(); return { status, preGeneration: true }; }; await handleRequest(f.event(), f.responder, f.deps); assert.equal(f.calls.head.status, status); assert.equal(f.calls.updates.length, 3); } });
test('an ambiguous upstream failure stays pending even with a 400 status', async () => { const f = fixture(); f.deps.forwardImpl = async p => { p.writeHead(400, { 'content-type': 'application/json' }); p.end(); return { status: 400, preGeneration: false }; }; await handleRequest(f.event(), f.responder, f.deps); assert.equal(f.calls.updates.length, 1); });
test('ambiguous upstream 500 keeps hold pending', async () => { const f = fixture(); f.deps.forwardImpl = async p => { p.writeHead(500, { 'content-type': 'application/json' }); p.end(); return { status: 500 }; }; await handleRequest(f.event(), f.responder, f.deps); assert.equal(f.calls.updates.length, 1); assert.equal(f.calls.puts[0].Item.state.S, 'pending'); });
test('missing request limits fail closed', async () => { const f = fixture(); f.deps.env = { ...env, MAX_OUTPUT_TOKENS: undefined }; await handleRequest(f.event(), f.responder, f.deps); assert.equal(f.calls.errors[0].status, 503); assert.equal(f.calls.forwards.length, 0); });
test('missing prompt context bound fails closed', async () => { const f = fixture(); f.deps.env = { ...env, MODEL_POLICY_JSON: JSON.stringify({ ...policy, max_prompt_tokens: undefined }) }; await handleRequest(f.event(), f.responder, f.deps); assert.equal(f.calls.errors[0].status, 503); assert.equal(f.calls.forwards.length, 0); });
test('text request bytes cannot exceed the reserved prompt-token bound', async () => { const f = fixture(); f.deps.env = { ...env, MODEL_POLICY_JSON: JSON.stringify({ ...policy, max_prompt_tokens: 20 }) }; await handleRequest(f.event(), f.responder, f.deps); assert.equal(f.calls.errors[0].status, 413); assert.equal(f.calls.forwards.length, 0); });
test('remote images and other multimodal parts are rejected', async () => { const f = fixture(); await handleRequest(f.event({ model: policy.models[0], messages: [{ role: 'user', content: [{ type: 'image_url', image_url: { url: 'https://example.com/huge.jpg' } }] }], stream: true }), f.responder, f.deps); assert.equal(f.calls.errors[0].status, 400); assert.equal(f.calls.forwards.length, 0); });
const png = 'data:image/png;base64,iVBORw0KGgo=';
const imageBody = (url = png) => ({ model: policy.models[0], messages: [{ role: 'user', content: [{ type: 'text', text: 'Describe' }, { type: 'image_url', image_url: { url } }] }], stream: true });
test('an inline image is routed to the policy vision model under the same price ceiling', async () => { const f = fixture(); f.deps.env = { ...env, MODEL_POLICY_JSON: JSON.stringify({ ...policy, vision_model: 'google/gemma-3-12b-it' }) }; await handleRequest(f.event(imageBody()), f.responder, f.deps); assert.equal(f.calls.errors.length, 0, f.calls.errors[0]?.message); assert.equal(f.calls.forwards[0].body.model, 'google/gemma-3-12b-it'); assert.deepEqual(f.calls.forwards[0].body.provider.max_price, policy.max_price); assert.equal(f.calls.puts[0].Item.model.S, 'google/gemma-3-12b-it'); const [line] = f.calls.logs; assert.deepEqual([line.model, line.image, line.request_id], ['google/gemma-3-12b-it', true, f.calls.puts[0].Item.request_id.S]); assert.equal(JSON.stringify(f.calls.logs).includes('base64'), false); });
test('images are refused without a policy vision model, and remote or video parts always', async () => { for (const [vision, body] of [[undefined, imageBody()], ['google/gemma-3-12b-it', imageBody('https://example.com/a.png')], ['google/gemma-3-12b-it', { ...imageBody(), messages: [{ role: 'user', content: [{ type: 'video_url', video_url: { url: 'data:video/mp4;base64,AAAA' } }] }] }]]) { const f = fixture(); f.deps.env = { ...env, MODEL_POLICY_JSON: JSON.stringify({ ...policy, vision_model: vision }) }; await handleRequest(f.event(body), f.responder, f.deps); assert.equal(f.calls.errors[0].status, 400); assert.equal(f.calls.forwards.length, 0); } });
test('client stream_options is admitted but the server usage flag wins', () => { const body = prepareBody({ model: policy.models[0], messages: [{ role: 'user', content: 'Hi' }], stream: true, stream_options: { include_usage: false } }, policy, 'owner-1', env); assert.deepEqual(body.stream_options, { include_usage: true }); });
test('server-side tools that can add unpriced spend are rejected', async () => { const f = fixture(); await handleRequest(f.event({ model: policy.models[0], messages: [{ role: 'user', content: 'Hi' }], stream: true, tools: [{ type: 'web_search' }] }), f.responder, f.deps); assert.equal(f.calls.errors[0].status, 400); assert.equal(f.calls.forwards.length, 0); });
// Raw ceiling 100000 x 0.5 + 8192 x 2 = 66384 microcents; x 1.2 markup = 79660.8, rounded up.
test('reservation covers policy prompt context and requested output at price ceilings, marked up', async () => { const f = fixture(); await handleRequest(f.event(), f.responder, f.deps); assert.equal(f.calls.puts[0].Item.reservation_microcents.N, '79661'); });
test('paid reservation fails closed without a markup', async () => { for (const markup of [undefined, 'abc', '0.9']) { const f = fixture(); f.deps.env = { ...env, OPENROUTER_MARKUP: markup }; await handleRequest(f.event(), f.responder, f.deps); assert.equal(f.calls.errors[0].status, 503); assert.equal(f.calls.forwards.length, 0); } });
test('zero-price policy is not held to the byte-for-token bound (a Hermes first turn is >100 KB)', async () => { const f = fixture({ balance: null, status: 'trial_local', provider: 'none' }); f.deps.env = { ...env, MAX_BODY_BYTES: '262144', MODEL_POLICY_JSON: JSON.stringify({ ...policy, models: ['nvidia/nemotron-3-ultra-550b-a55b:free'], max_price: { prompt: 0, completion: 0 }, max_prompt_tokens: 100000 }) }; await handleRequest(f.event({ model: 'nvidia/nemotron-3-ultra-550b-a55b:free', messages: [{ role: 'system', content: 'x'.repeat(150000) }, { role: 'user', content: 'Hi' }], stream: true }), f.responder, f.deps); assert.equal(f.calls.errors.length, 0, f.calls.errors[0]?.message); assert.equal(f.calls.forwards.length, 1); });
test('zero-price requests still stop at the hard body cap', async () => { const f = fixture({ balance: null, status: 'trial_local', provider: 'none' }); f.deps.env = { ...env, MAX_BODY_BYTES: '262144', MODEL_POLICY_JSON: JSON.stringify({ ...policy, models: ['nvidia/nemotron-3-ultra-550b-a55b:free'], max_price: { prompt: 0, completion: 0 } }) }; await handleRequest(f.event({ model: 'nvidia/nemotron-3-ultra-550b-a55b:free', messages: [{ role: 'user', content: 'x'.repeat(300000) }], stream: true }), f.responder, f.deps); assert.equal(f.calls.errors[0].status, 413); assert.equal(f.calls.forwards.length, 0); });

// Cheap paid model for capped owners: OpenRouter's per-key daily $ limit is the valve.
const deepseek = { revision: 'deepseek-v4-flash-trial-v1', models: ['deepseek/deepseek-v4-flash'], max_prompt_tokens: 400000, max_price: { prompt: 0.1, completion: 0.2 } };
const dsEnv = { ...env, MAX_BODY_BYTES: '262144', GATE_ENV: 'dev', MODEL_POLICY_JSON: JSON.stringify(deepseek) };
const dsBody = (content = 'Hi') => ({ model: 'deepseek/deepseek-v4-flash', messages: [{ role: 'user', content }], stream: true });
const today = () => new Date().toISOString().slice(0, 10);
const cappedOwners = { trial: { status: 'trial_local', provider: null }, base: { plan: 'base', provider: null }, paid_default: { plan: 'standard', provider: null }, paid_default_ultra: { plan: 'ultra_pro', status: 'trialing', provider: null } };
async function runDs(options, body = dsBody(), setup = () => {}) { const f = fixture({ balance: null, metronome: false, ...options }); f.deps.env = dsEnv; setup(f); await handleRequest(f.event(body), f.responder, f.deps); return f; }

test('capped owners are admitted on a priced policy with no provider choice, Metronome contract, or reservation', async () => {
  for (const [name, options] of Object.entries(cappedOwners)) {
    const f = await runDs(options);
    assert.equal(f.calls.errors.length, 0, `${name}: ${f.calls.errors[0]?.message}`);
    assert.equal(f.calls.forwards.length, 1, name);
    assert.equal(f.calls.puts.length, 1, name);
    assert.equal(f.calls.puts[0].Item.billing_mode.S, 'capped', name);
    assert.equal(f.calls.puts[0].Item.reservation_microcents.N, '0', name);
    assert.equal(f.calls.forwards[0].body.provider.data_collection, 'deny', name);
  }
});
test('a paid plan without a provider choice is not capped once its subscription lapses', async () => { const f = await runDs({ plan: 'standard', provider: null, status: 'canceled' }); assert.equal(f.calls.errors[0].status, 403); assert.equal(f.calls.forwards.length, 0); });
test('a paid plan that explicitly chose managed keeps the Metronome-reserved path', async () => { const f = fixture({ plan: 'standard' }); f.deps.env = dsEnv; await handleRequest(f.event(dsBody()), f.responder, f.deps); assert.equal(f.calls.errors.length, 0, f.calls.errors[0]?.message); assert.equal(f.calls.puts[0].Item.billing_mode.S, 'paid'); assert.notEqual(f.calls.puts[0].Item.reservation_microcents.N, '0'); });
test('capped requests are bounded by MAX_BODY_BYTES alone, not the byte-for-token bound', async () => {
  const setup = f => { f.deps.env = { ...dsEnv, MODEL_POLICY_JSON: JSON.stringify({ ...deepseek, max_prompt_tokens: 1000 }) }; };
  const ok = await runDs(cappedOwners.trial, dsBody('x'.repeat(150000)), setup);
  assert.equal(ok.calls.errors.length, 0, ok.calls.errors[0]?.message);
  const big = await runDs(cappedOwners.trial, dsBody('x'.repeat(300000)), setup);
  assert.equal(big.calls.errors[0].status, 413);
  const paid = fixture({ plan: 'standard' }); setup(paid); await handleRequest(paid.event(dsBody('x'.repeat(2000))), paid.responder, paid.deps);
  assert.equal(paid.calls.errors[0].status, 413);
});
test('a capped definite pre-generation failure releases the outbox row without a Metronome hold', async () => {
  const f = await runDs(cappedOwners.trial, dsBody(), f => { f.deps.forwardImpl = async p => { p.writeHead(400, { 'content-type': 'application/json' }); p.end(); return { status: 400, preGeneration: true }; }; });
  assert.equal(f.calls.updates.length, 1);
  assert.equal(f.calls.updates[0].TableName, 'requests');
  assert.equal(f.calls.updates[0].ExpressionAttributeValues[':released'].S, 'released');
});
test('prepareBody passes the policy provider routing through and keeps deny', () => {
  const routed = { ...deepseek, provider: { order: ['deepinfra', 'azure'], ignore: ['baidu'], allow_fallbacks: true } };
  assert.deepEqual(prepareBody(dsBody(), routed, 'owner-1', env).provider, { max_price: deepseek.max_price, data_collection: 'deny', order: ['deepinfra', 'azure'], ignore: ['baidu'], allow_fallbacks: true });
  assert.deepEqual(prepareBody(dsBody(), deepseek, 'owner-1', env).provider, { max_price: deepseek.max_price, data_collection: 'deny' });
  for (const provider of [{ order: 'deepinfra' }, { ignore: [1] }, { allow_fallbacks: 'yes' }, { data_collection: 'allow' }]) assert.throws(() => prepareBody(dsBody(), { ...deepseek, provider }, 'owner-1', env), e => e.status === 503);
});
const upstream402 = (keyInfoImpl) => f => { f.deps.keyInfoImpl = keyInfoImpl; f.deps.forwardImpl = async p => { p.writeHead(402, { 'content-type': 'application/json' }); p.write(Buffer.from('{"error":"insufficient credits"}')); p.end(); return { status: 402, preGeneration: true }; }; };
test('a capped 402 on an empty account (the key has money left) is the paging 503, never a day-long cap', async () => {
  const f = await runDs(cappedOwners.trial, dsBody(), upstream402(async () => ({ limit: 0.25, limit_remaining: 0.25, usage_daily: 0 })));
  assert.equal(f.calls.head.status, 503);
  assert.equal(JSON.parse(Buffer.concat(f.calls.chunks).toString()).error.code, 'provider_unavailable');
  assert.equal(f.calls.updates.some(u => u.TableName === 'billing'), false);
  assert.ok(f.calls.logs.some(entry => entry['openrouter.gate.account_refused'] === 1));
});
const liveKeyLimit = '{"error":{"message":"Key limit exceeded (daily limit). Manage it using https://openrouter.ai/settings/keys","code":403}}';
const keyLimit403 = (keyInfoImpl) => f => { f.deps.keyInfoImpl = keyInfoImpl; f.deps.forwardImpl = async p => { p.writeHead(403, { 'content-type': 'application/json' }); p.write(Buffer.from('{"error":{"message":"Upstream returned 403"}}')); p.end(liveKeyLimit); return { status: 403, preGeneration: true }; }; };
// OpenRouter's documented 402 shape (docs/api-reference/limits), with a key and prompt text planted to prove neither is logged.
const refusal402 = JSON.stringify({ error: { code: 402, message: `This request requires more credits, or fewer max_tokens. sk-or-secret ${'x'.repeat(1000)}`, metadata: { limit_source: 'openrouter_key_limit', scope: 'key_limit', reason: 'key_limit', flagged_input: 'PROMPT-TEXT' } } });
const body402 = (keyInfoImpl) => f => { f.deps.keyInfoImpl = keyInfoImpl; f.deps.forwardImpl = async p => { p.writeHead(402, { 'content-type': 'application/json' }); p.write(Buffer.from('{"error":{"message":"Upstream returned 402"}}')); p.end(refusal402); return { status: 402, preGeneration: true }; }; };
const refusalLine = f => f.calls.logs.find(entry => entry.upstream_status);
test('a capped 402 with less daily allowance left than the request could cost is the daily cap, stamped', async () => {
  const f = await runDs(cappedOwners.paid_default, dsBody(), body402(async () => ({ limit: 0.25, limit_remaining: 0.004 })));
  assert.equal(f.calls.head.status, 429);
  assert.equal(JSON.parse(Buffer.concat(f.calls.chunks).toString()).error.code, 'trial_daily_cap');
  assert.equal(f.calls.updates.find(u => u.TableName === 'billing').ExpressionAttributeValues[':day'].S, today());
  assert.ok(f.calls.logs.some(entry => entry['openrouter.gate.cap_hit'] === 1));
  assert.equal(f.calls.logs.some(entry => entry['openrouter.gate.account_refused'] === 1), false);
});
test('the affordability line follows the request: dearer output prices a larger remainder into the cap', async () => {
  const dear = f => { f.deps.env = { ...dsEnv, MODEL_POLICY_JSON: JSON.stringify({ ...deepseek, max_price: { prompt: 0.1, completion: 2 } }) }; };
  const capped = await runDs(cappedOwners.base, dsBody(), f => { dear(f); body402(async () => ({ limit: 0.25, limit_remaining: 0.012 }))(f); });
  assert.equal(capped.calls.head.status, 429);
  const paging = await runDs(cappedOwners.base, dsBody(), body402(async () => ({ limit: 0.25, limit_remaining: 0.012 })));
  assert.equal(paging.calls.head.status, 503);
});
test('a capped 402 with real daily allowance left stays the paging 503, and says why', async () => {
  const f = await runDs(cappedOwners.paid_default, dsBody(), body402(async () => ({ limit: 0.25, limit_remaining: 0.2 })));
  assert.equal(f.calls.head.status, 503);
  assert.equal(JSON.parse(Buffer.concat(f.calls.chunks).toString()).error.code, 'provider_unavailable');
  assert.equal(f.calls.updates.some(u => u.TableName === 'billing'), false);
  assert.ok(f.calls.logs.some(entry => entry['openrouter.gate.account_refused'] === 1));
  assert.equal(refusalLine(f).limit_remaining, 0.2);
});
test('a capped 402 whose key read fails is the 503, with no stamp', async () => {
  const f = await runDs(cappedOwners.paid_default, dsBody(), body402(async () => { throw new Error('timeout'); }));
  assert.equal(f.calls.head.status, 503);
  assert.equal(f.calls.updates.some(u => u.TableName === 'billing'), false);
  assert.ok(f.calls.logs.some(entry => entry['openrouter.gate.account_refused'] === 1));
  assert.equal(refusalLine(f).limit_remaining, null);
});
test('a capped 402 on a key with no limit (limit_remaining null) is the account, not a spent key', async () => {
  const f = await runDs(cappedOwners.base, dsBody(), body402(async () => ({ limit: null, limit_remaining: null })));
  assert.equal(f.calls.head.status, 503);
  assert.equal(f.calls.updates.some(u => u.TableName === 'billing'), false);
  assert.equal(refusalLine(f).limit_remaining, null);
});
test('the refusal line carries the reason fields, a capped message, and no key or prompt', async () => {
  const f = await runDs(cappedOwners.paid_default, dsBody('PROMPT-TEXT'), body402(async () => ({ limit: 0.25, limit_remaining: 0.2 })));
  const line = refusalLine(f);
  assert.equal(line.upstream_status, 402);
  assert.equal(line.upstream_code, '402');
  assert.equal(line.limit_source, 'openrouter_key_limit');
  assert.equal(line.scope, 'key_limit');
  assert.ok(line.upstream_message.startsWith('This request requires more credits'));
  assert.ok(line.upstream_message.length <= 300);
  const logged = JSON.stringify(f.calls.logs);
  assert.equal(logged.includes('sk-or-'), false);
  assert.equal(logged.includes('PROMPT-TEXT'), false);
});
test('a key-limit 403 logs its refusal the same way; an unparseable body still logs the status', async () => {
  const f = await runDs(cappedOwners.trial, dsBody(), keyLimit403(async () => ({ limit: 0.25, limit_remaining: 0.2485 })));
  assert.equal(refusalLine(f).upstream_status, 403);
  assert.ok(refusalLine(f).upstream_message.startsWith('Key limit exceeded'));
  const bare = await runDs(cappedOwners.trial, dsBody(), upstream402(async () => ({ limit: 0.25, limit_remaining: 0.2 })));
  assert.equal(refusalLine(bare).upstream_status, 402);
  assert.equal(refusalLine(bare).limit_source, null);
});
test('"Key limit exceeded" on a key that really spent its day is the daily cap', async () => {
  const f = await runDs(cappedOwners.trial, dsBody(), keyLimit403(async () => ({ limit: 0.25, limit_remaining: 0.001 })));
  assert.equal(f.calls.head.status, 429);
  assert.equal(JSON.parse(Buffer.concat(f.calls.chunks).toString()).error.code, 'trial_daily_cap');
  assert.equal(f.calls.updates.find(u => u.TableName === 'billing').ExpressionAttributeValues[':day'].S, today());
});
test('"Key limit exceeded" on a key with money left (an empty account) is a 503, not a day-long cap', async () => {
  const f = await runDs(cappedOwners.trial, dsBody(), keyLimit403(async () => ({ limit: 0.25, limit_remaining: 0.2485 })));
  assert.equal(f.calls.head.status, 503);
  assert.equal(JSON.parse(Buffer.concat(f.calls.chunks).toString()).error.code, 'provider_unavailable');
  assert.equal(f.calls.updates.some(u => u.TableName === 'billing'), false);
  assert.ok(f.calls.logs.some(entry => entry['openrouter.gate.account_refused'] === 1));
  assert.equal(f.calls.logs.some(entry => entry['openrouter.gate.cap_hit'] === 1), false);
});
test('a paid managed owner on an empty account is a 503 and counts toward the page', async () => {
  const f = fixture({ plan: 'standard' }); f.deps.env = dsEnv; keyLimit403(async () => ({ limit: 50, limit_remaining: 49.9 }))(f);
  await handleRequest(f.event(dsBody()), f.responder, f.deps);
  assert.equal(f.calls.head.status, 503);
  assert.ok(f.calls.logs.some(entry => entry['openrouter.gate.account_refused'] === 1 && entry.tier === 'managed_paid'));
  assert.equal(f.calls.updates.some(u => u.TableName === 'billing'), false);
});
test('a paid managed owner past its backstop limit still gets the upstream 403', async () => {
  const f = fixture({ plan: 'standard' }); f.deps.env = dsEnv; keyLimit403(async () => ({ limit: 50, limit_remaining: 0 }))(f);
  await handleRequest(f.event(dsBody()), f.responder, f.deps);
  assert.equal(f.calls.head.status, 403);
  assert.equal(f.calls.logs.some(entry => entry['openrouter.gate.account_refused'] === 1), false);
});
test('a failed key check never stamps a whole day', async () => {
  const f = await runDs(cappedOwners.trial, dsBody(), keyLimit403(async () => { throw new Error('timeout'); }));
  assert.equal(f.calls.head.status, 503);
  assert.equal(f.calls.updates.some(u => u.TableName === 'billing'), false);
});
test('any other capped 403 (e.g. a disabled key) passes through unstamped', async () => {
  const f = await runDs(cappedOwners.trial, dsBody(), f => { f.deps.forwardImpl = async p => { p.writeHead(403, { 'content-type': 'application/json' }); p.write(Buffer.from('{"error":{"message":"Upstream returned 403"}}')); p.end('{"error":{"message":"Key is disabled","code":403}}'); return { status: 403, preGeneration: true }; }; });
  assert.equal(f.calls.head.status, 403);
  assert.equal(JSON.parse(Buffer.concat(f.calls.chunks).toString()).error.message, 'Upstream returned 403');
  assert.equal(f.calls.updates.some(u => u.TableName === 'billing'), false);
});
test('a capped owner already stamped today is refused before upstream', async () => {
  const f = await runDs({ ...cappedOwners.trial, cappedDay: today() });
  assert.equal(f.calls.errors[0].status, 429);
  assert.equal(JSON.parse(f.calls.errors[0].body()).error.code, 'trial_daily_cap');
  assert.equal(f.calls.forwards.length, 0);
  assert.equal(f.calls.puts.length, 0);
  const yesterday = await runDs({ ...cappedOwners.trial, cappedDay: '2000-01-01' });
  assert.equal(yesterday.calls.forwards.length, 1);
});
test('an explicit managed owner 402 is passed through, not stamped', async () => { const f = fixture({ plan: 'standard' }); f.deps.env = dsEnv; f.deps.forwardImpl = async p => { p.writeHead(402, { 'content-type': 'application/json' }); p.end(); return { status: 402, preGeneration: true }; }; await handleRequest(f.event(dsBody()), f.responder, f.deps); assert.equal(f.calls.head.status, 402); assert.equal(f.calls.updates.some(u => u.TableName === 'billing'), false); });
test('each admitted request emits one EMF request metric with tier and billing mode', async () => {
  const expected = { trial: ['trial', 'capped'], base: ['base', 'capped'], paid_default: ['paid_default', 'capped'] };
  for (const [name, [tier, mode]] of Object.entries(expected)) {
    const f = await runDs(cappedOwners[name]);
    assert.equal(f.calls.logs.length, 1, name);
    const [log] = f.calls.logs;
    assert.deepEqual(log._aws.CloudWatchMetrics[0], { Namespace: 'LightbulbAI', Dimensions: [['env', 'tier', 'billing_mode']], Metrics: [{ Name: 'openrouter.gate.request', Unit: 'Count' }] });
    assert.deepEqual([log.env, log.tier, log.billing_mode, log.image, log['openrouter.gate.request']], ['dev', tier, mode, false, 1]);
    assert.equal(typeof log.model, 'string');
  }
  const paid = fixture({ plan: 'standard' }); paid.deps.env = dsEnv; await handleRequest(paid.event(dsBody()), paid.responder, paid.deps);
  assert.deepEqual([paid.calls.logs[0].tier, paid.calls.logs[0].billing_mode], ['managed_paid', 'paid']);
  const refused = await runDs({ plan: 'standard', provider: null, status: 'canceled' });
  assert.equal(refused.calls.logs.length, 1);
  assert.equal(refused.calls.logs[0].status, 403);
});

test('an expired trial is refused before any priced upstream call', async () => { const f = fixture({ balance: null, status: 'trial_local', provider: 'none', metronome: false, trialEnd: Math.floor(Date.now() / 1000) - 60 }); f.deps.env = { ...env, MODEL_POLICY_JSON: JSON.stringify(deepseek) }; await handleRequest(f.event({ model: 'deepseek/deepseek-v4-flash', messages: [{ role: 'user', content: 'Hi' }], stream: true }), f.responder, f.deps); assert.equal(f.calls.errors[0].status, 403); assert.equal(f.calls.forwards.length, 0); });
test('an unexpired trial is still admitted on the capped path', async () => { const f = fixture({ balance: null, status: 'trial_local', provider: 'none', metronome: false, trialEnd: Math.floor(Date.now() / 1000) + 86400 }); f.deps.env = { ...env, MODEL_POLICY_JSON: JSON.stringify(deepseek) }; await handleRequest(f.event({ model: 'deepseek/deepseek-v4-flash', messages: [{ role: 'user', content: 'Hi' }], stream: true }), f.responder, f.deps); assert.equal(f.calls.errors.length, 0, f.calls.errors[0]?.message); assert.equal(f.calls.forwards.length, 1); });
test('a lapsed Base subscription is not admitted on the capped path', async () => { for (const status of ['canceled', 'past_due']) { const f = fixture({ balance: null, status, provider: 'none', plan: 'base', metronome: false }); f.deps.env = { ...env, MODEL_POLICY_JSON: JSON.stringify(deepseek) }; await handleRequest(f.event({ model: 'deepseek/deepseek-v4-flash', messages: [{ role: 'user', content: 'Hi' }], stream: true }), f.responder, f.deps); assert.equal(f.calls.errors[0].status, 403, status); assert.equal(f.calls.forwards.length, 0); } });

// Hermes's "custom" provider probes an OpenAI-shaped models list at either of these paths
// (agent/model_metadata.py fetch_endpoint_model_metadata) before it ever sends a chat request.
const modelsEvent = (path, token = 'good') => ({ rawPath: path, requestContext: { http: { method: 'GET' } }, headers: { authorization: `Bearer ${token}` } });
test('an authorized GET lists the policy models in OpenAI shape', async () => {
  for (const path of ['/v1/models', '/models']) {
    const f = fixture();
    await handleRequest(modelsEvent(path), f.responder, f.deps);
    assert.equal(f.calls.errors.length, 0, f.calls.errors[0]?.message);
    assert.equal(f.calls.head.status, 200, path);
    const parsed = JSON.parse(Buffer.concat(f.calls.chunks).toString());
    assert.deepEqual(parsed, { object: 'list', data: policy.models.map(id => ({ id, object: 'model', owned_by: 'lightbulb' })) });
    assert.equal(f.calls.forwards.length, 0);
  }
});
test('an unauthorized GET to the models list is refused like chat', async () => {
  const f = fixture({ token: 'wrong' });
  await handleRequest(modelsEvent('/v1/models'), f.responder, f.deps);
  assert.equal(f.calls.errors[0].status, 401);
  assert.equal(f.calls.head, undefined);
});
test('every other path still 404s', async () => {
  const f = fixture();
  await handleRequest(modelsEvent('/v1/embeddings'), f.responder, f.deps);
  assert.equal(f.calls.errors[0].status, 404);
});

test('auto-routed request records the model that answered, and an unlisted model is still refused', async () => {
  const f = fixture();
  f.deps.forwardImpl = async p => { p.writeHead(200, { 'content-type': 'text/event-stream' }); p.end(); return { status: 200, generationId: 'gen-1', model: 'anthropic/claude-sonnet-4.5' }; };
  await handleRequest(f.event({ model: 'openrouter/auto', messages: [{ role: 'user', content: 'Hi' }], stream: true }), f.responder, f.deps);
  assert.equal(f.calls.errors.length, 0);
  assert.ok(f.calls.updates.some(u => u.ExpressionAttributeValues?.[':model']?.S === 'anthropic/claude-sonnet-4.5'));
  const g = fixture();
  await handleRequest(g.event({ model: 'evil/model', messages: [{ role: 'user', content: 'Hi' }], stream: true }), g.responder, g.deps);
  assert.equal(g.calls.errors[0].status, 400);
  assert.equal(g.calls.errors[0].message, 'Model not allowed');
});

// Real cost x markup stamped on the requests row (microcents = USD x 1e6 x markup, as settlement's debit_microcents).
const costStamp = f => f.calls.updates.find(u => u.ExpressionAttributeValues?.[':cost']);
test('a completed stream stamps cost_microcents = ceil(usage.cost x 1e6 x markup) on the requests row', async () => {
  const f = fixture();
  f.deps.forwardImpl = async p => { p.writeHead(200, { 'content-type': 'text/event-stream' }); p.end(); return { status: 200, generationId: 'gen-1', cost: 0.00123 }; };
  await handleRequest(f.event(), f.responder, f.deps);
  const u = costStamp(f);
  assert.equal(u.TableName, 'requests');
  assert.match(u.UpdateExpression, /cost_microcents/);
  assert.equal(u.ExpressionAttributeValues[':cost'].N, '1476'); // 1230 x 1.2
});
test('a stream cut before the usage chunk leaves cost_microcents absent, never 0', async () => {
  const f = fixture();
  await handleRequest(f.event(), f.responder, f.deps); // default forwardImpl returns no cost
  assert.equal(costStamp(f), undefined);
  assert.equal(JSON.stringify(f.calls.updates).includes('cost_microcents'), false);
});
test('the requests row carries agent_id for a per-agent bearer and omits it for an owner bearer', async () => {
  const f = fixture();
  const get = f.deps.ddb.getItem;
  f.deps.ddb.getItem = async p => p.TableName === 'tokens' ? { Item: { owner_id: AV('owner-1'), agent_id: AV('agent-7') } } : get(p);
  await handleRequest(f.event(), f.responder, f.deps);
  assert.equal(f.calls.puts[0].Item.agent_id.S, 'agent-7');
  const g = fixture();
  await handleRequest(g.event(), g.responder, g.deps);
  assert.equal('agent_id' in g.calls.puts[0].Item, false);
});
// OpenRouter says to branch on limit_source: only openrouter_key_limit is the key's cap.
const sourced402 = (source, keyInfoImpl) => f => { f.deps.keyInfoImpl = keyInfoImpl; f.deps.forwardImpl = async p => { p.writeHead(402, { 'content-type': 'application/json' }); p.write(Buffer.from('{}')); p.end(JSON.stringify({ error: { code: 402, message: 'refused', metadata: { limit_source: source } } })); return { status: 402, preGeneration: true }; }; };
const setBody = (f, bytes) => { f.deps.env = { ...dsEnv, MAX_BODY_BYTES: String(bytes + 10000) }; };
for (const source of ['openrouter_credits', 'openrouter_in_flight_budget']) {
  test(`a capped 402 from ${source} is the paging 503 even under the line`, async () => {
    const f = await runDs(cappedOwners.trial, dsBody(), sourced402(source, async () => ({ limit: 0.25, limit_remaining: 0.004 })));
    assert.equal(f.calls.head.status, 503);
    assert.equal(f.calls.updates.some(u => u.TableName === 'billing'), false);
    assert.ok(f.calls.logs.some(entry => entry['openrouter.gate.account_refused'] === 1));
  });
}
test('a capped 402 from openrouter_key_limit under the line is the daily cap', async () => {
  const f = await runDs(cappedOwners.trial, dsBody(), sourced402('openrouter_key_limit', async () => ({ limit: 0.25, limit_remaining: 0.004 })));
  assert.equal(f.calls.head.status, 429);
  assert.equal(f.calls.updates.find(u => u.TableName === 'billing').ExpressionAttributeValues[':day'].S, today());
});
test('a 5 MB body at $0.12 left with an in-flight 402 is the 503, never a stamped day', async () => {
  const f = await runDs(cappedOwners.trial, dsBody('x'.repeat(5000000)), f => { setBody(f, 5200000); sourced402('openrouter_in_flight_budget', async () => ({ limit: 0.25, limit_remaining: 0.12 }))(f); });
  assert.equal(f.calls.errors.length, 0, f.calls.errors[0]?.message);
  assert.equal(f.calls.head.status, 503);
  assert.equal(f.calls.updates.some(u => u.TableName === 'billing'), false);
});
test('the ceiling counts the prompt: a 400 KB body lifts the line above $0.011', async () => {
  const f = await runDs(cappedOwners.trial, dsBody('x'.repeat(400000)), f => { setBody(f, 400000); body402(async () => ({ limit: 0.25, limit_remaining: 0.011 }))(f); });
  assert.equal(f.calls.errors.length, 0, f.calls.errors[0]?.message);
  assert.equal(f.calls.head.status, 429);
});
test('a paid owner\'s 403 at $0.012 left stays the 503: the ceiling is for capped owners only', async () => {
  const f = fixture({ plan: 'standard' }); f.deps.env = { ...dsEnv, MODEL_POLICY_JSON: JSON.stringify({ ...deepseek, max_price: { prompt: 0.1, completion: 2 } }) }; keyLimit403(async () => ({ limit: 50, limit_remaining: 0.012 }))(f);
  await handleRequest(f.event(dsBody()), f.responder, f.deps);
  assert.equal(f.calls.head.status, 503);
});
