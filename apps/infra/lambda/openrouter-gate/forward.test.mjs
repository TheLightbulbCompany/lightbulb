import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { forward } from './forward.mjs';

test('forwards split text and tool SSE unchanged and extracts generation ID', async () => {
  const chunks = [Buffer.from('data: {"id":"gen-12","choices":[{"delta":{"content":"hel'), Buffer.from('lo"}}]}\n\ndata: {"choices":[{"delta":{"tool_calls":[{"function":{"arguments":"{}"}}]}}]}\n\n')];
  const written = [];
  const requestImpl = (_url, options, onResponse) => {
    assert.equal(options.headers.authorization, 'Bearer sk-or-secret');
    const req = new EventEmitter();
    req.end = () => {
      const upstream = new EventEmitter();
      upstream.statusCode = 200;
      upstream.headers = { 'content-type': 'text/event-stream' };
      onResponse(upstream);
      queueMicrotask(() => { for (const chunk of chunks) upstream.emit('data', chunk); upstream.emit('end'); });
    };
    return req;
  };
  const result = await forward({ body: { stream: true }, key: 'sk-or-secret', requestImpl, writeHead: () => {}, write: chunk => written.push(chunk), end: () => {} });
  assert.deepEqual(written, chunks);
  assert.equal(result.generationId, 'gen-12');
});

test('persists generation before writing and pauses upstream on client backpressure', async () => {
  const order = [];
  let drain;
  const upstream = new EventEmitter();
  upstream.statusCode = 200;
  upstream.headers = { 'content-type': 'text/event-stream' };
  upstream.queue = [];
  upstream.paused = false;
  upstream.pause = () => { upstream.paused = true; order.push('pause'); };
  upstream.resume = () => { upstream.paused = false; order.push('resume'); flush(); };
  function flush() { while (!upstream.paused && upstream.queue.length) upstream.emit('data', upstream.queue.shift()); if (!upstream.paused && upstream.finished) upstream.emit('end'); }
  const requestImpl = (_url, _options, onResponse) => {
    const req = new EventEmitter();
    req.end = () => { onResponse(upstream); queueMicrotask(() => { upstream.queue.push(Buffer.from('data: {"id":"gen-1"}\n\n'), Buffer.from('data: [DONE]\n\n')); upstream.finished = true; flush(); }); };
    return req;
  };
  const resultPromise = forward({ body: {}, key: 'secret', requestImpl, writeHead: () => {},
    onGenerationId: async id => { assert.equal(id, 'gen-1'); order.push('persist'); },
    write: () => { order.push('write'); return order.filter(x => x === 'write').length > 1; },
    waitForDrain: () => new Promise(resolve => { drain = resolve; }), end: () => {} });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(typeof drain, 'function');
  assert.deepEqual(order.slice(0, 3), ['pause', 'persist', 'write']);
  drain();
  const result = await resultPromise;
  assert.equal(result.generationId, 'gen-1');
  assert.equal(order.filter(x => x === 'write').length, 2);
});

test('persists a generation ID in a partial first SSE event before disconnect', async () => {
  let persisted = false;
  const requestImpl = (_url, _options, onResponse) => {
    const req = new EventEmitter();
    req.destroy = error => req.emit('error', error);
    req.end = () => {
      const upstream = new EventEmitter();
      upstream.statusCode = 200;
      upstream.headers = { 'content-type': 'text/event-stream' };
      onResponse(upstream);
      queueMicrotask(() => upstream.emit('data', Buffer.from('data: {"id":"gen-1","choices":[{"delta":{"content":"hel')));
    };
    return req;
  };
  await assert.rejects(forward({ body: {}, key: 'secret', requestImpl, writeHead: () => {},
    onGenerationId: async () => { persisted = true; },
    write: () => { throw new Error('disconnect'); }, end: () => {} }), /disconnect/);
  assert.equal(persisted, true);
});

test('completed upstream 401 is explicitly classified as pre-generation', async () => {
  const requestImpl = (_url, _options, onResponse) => {
    const req = new EventEmitter();
    req.end = () => { const upstream = new EventEmitter(); upstream.statusCode = 401; upstream.headers = { 'content-type': 'application/json' }; onResponse(upstream); queueMicrotask(() => upstream.emit('end')); };
    return req;
  };
  const result = await forward({ body: {}, key: 'secret', requestImpl, writeHead: () => {}, write: () => true, end: () => {} });
  assert.equal(result.preGeneration, true);
  assert.equal(result.generationId, undefined);
});

test('completed 403, 404, and 422 responses are pre-generation', async () => {
  for (const status of [403, 404, 422]) {
    const requestImpl = (_url, _options, onResponse) => {
      const req = new EventEmitter();
      req.end = () => { const upstream = new EventEmitter(); upstream.statusCode = status; upstream.headers = { 'content-type': 'application/json' }; onResponse(upstream); queueMicrotask(() => upstream.emit('end')); };
      return req;
    };
    const result = await forward({ body: {}, key: 'secret', requestImpl, writeHead: () => {}, write: () => true, end: () => {} });
    assert.equal(result.preGeneration, true, `status ${status}`);
    assert.equal(result.generationId, undefined);
  }
});

test('transport error waits for in-flight generation persistence', async () => {
  let finishPersist, persisted = false, rejected = false;
  const requestImpl = (_url, _options, onResponse) => {
    const req = new EventEmitter();
    req.end = () => {
      const upstream = new EventEmitter();
      upstream.statusCode = 200;
      upstream.headers = { 'content-type': 'text/event-stream' };
      onResponse(upstream);
      queueMicrotask(() => { upstream.emit('data', Buffer.from('data: {"id":"gen-1"}\n\n')); queueMicrotask(() => req.emit('error', new Error('reset'))); });
    };
    return req;
  };
  const outcome = forward({ body: {}, key: 'secret', requestImpl, writeHead: () => {}, write: () => true, end: () => {},
    onGenerationId: () => new Promise(resolve => { finishPersist = () => { persisted = true; resolve(); }; }) });
  outcome.catch(() => { rejected = true; });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(rejected, false);
  finishPersist();
  await assert.rejects(outcome, /reset/);
  assert.equal(persisted, true);
});

test('withholds a partial SSE prefix until its generation ID is durable', async () => {
  const order = [];
  const requestImpl = (_url, _options, onResponse) => {
    const req = new EventEmitter();
    req.end = () => {
      const upstream = new EventEmitter();
      upstream.statusCode = 200;
      upstream.headers = { 'content-type': 'text/event-stream' };
      onResponse(upstream);
      queueMicrotask(() => { upstream.emit('data', Buffer.from('data: {')); upstream.emit('data', Buffer.from('"id":"gen-1","choices":[]}\n\n')); upstream.emit('end'); });
    };
    return req;
  };
  await forward({ body: {}, key: 'secret', requestImpl, writeHead: () => {},
    onGenerationId: async () => { order.push('persist'); }, write: () => { order.push('write'); return true; }, end: () => {} });
  assert.deepEqual(order, ['persist', 'write', 'write']);
});

test('a non-200 upstream body reaches end() (bounded) while the client still gets the generic error', async () => {
  const live = '{"error":{"message":"Key limit exceeded (daily limit).","code":403}}';
  const written = [];
  let ended;
  const requestImpl = (_url, _options, onResponse) => {
    const req = new EventEmitter();
    req.end = () => {
      const upstream = new EventEmitter();
      upstream.statusCode = 403;
      upstream.headers = { 'content-type': 'application/json' };
      onResponse(upstream);
      queueMicrotask(() => { upstream.emit('data', Buffer.from(live)); upstream.emit('end'); });
    };
    return req;
  };
  const result = await forward({ body: {}, key: 'sk-or-secret', requestImpl, writeHead: () => {}, write: chunk => written.push(chunk), end: body => { ended = body; } });
  assert.equal(ended, live);
  assert.equal(JSON.parse(Buffer.concat(written).toString()).error.message, 'Upstream returned 403');
  assert.equal(result.preGeneration, true);
});

test('returns the model OpenRouter reports on the stream', async () => {
  const requestImpl = (_url, _options, onResponse) => {
    const req = new EventEmitter();
    req.end = () => {
      const upstream = new EventEmitter();
      upstream.statusCode = 200;
      upstream.headers = {};
      onResponse(upstream);
      queueMicrotask(() => { upstream.emit('data', Buffer.from('data: {"id":"gen-7","model":"anthropic/claude-sonnet-4.5","choices":[]}\n\n')); upstream.emit('end'); });
    };
    return req;
  };
  const result = await forward({ body: { stream: true }, key: 'k', requestImpl, writeHead: () => {}, write: () => {}, end: () => {} });
  assert.equal(result.model, 'anthropic/claude-sonnet-4.5');
});

const streamOf = chunks => (_url, _options, onResponse) => {
  const req = new EventEmitter();
  req.end = () => {
    const upstream = new EventEmitter();
    upstream.statusCode = 200;
    upstream.headers = {};
    onResponse(upstream);
    queueMicrotask(async () => { for (const c of chunks) { upstream.emit('data', Buffer.from(c)); await new Promise(r => setImmediate(r)); } upstream.emit('end'); });
  };
  return req;
};
const run = chunks => forward({ body: { stream: true }, key: 'k', requestImpl: streamOf(chunks), writeHead: () => {}, write: () => {}, end: () => {} });

test('returns usage.cost from the final usage chunk, even when it is split across reads', async () => {
  const result = await run(['data: {"id":"gen-8","choices":[{"delta":{"content":"hi"}}]}\n\n', 'data: {"id":"gen-8","choices":[],"usage":{"prompt_tokens":5,"cost_details":{"upstream_inference_cost":0.9},"cost":0.00', '123}}\n\ndata: [DONE]\n\n']);
  assert.equal(result.cost, 0.00123);
});

test('a stream cut before the usage chunk reports no cost (undefined, never 0)', async () => {
  const result = await run(['data: {"id":"gen-9","choices":[{"delta":{"content":"hi"}}]}\n\n']);
  assert.equal(result.cost, undefined);
});
