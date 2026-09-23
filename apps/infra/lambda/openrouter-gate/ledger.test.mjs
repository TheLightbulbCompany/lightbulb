import test from 'node:test';
import assert from 'node:assert/strict';
import { reserve, release } from './ledger.mjs';

test('concurrent reservations cannot both claim the same balance', async () => {
  let reserved = 0;
  const ddb = {
    async getItem() { return { Item: { remaining_microcents: { N: '500' }, reserved_microcents: { N: '0' }, openrouter_reserved_microcents: { N: String(reserved) } } }; },
    async transactWriteItems(params) {
      const values = params.TransactItems[0].Update.ExpressionAttributeValues;
      if (Number(values[':openrouter'].N) !== reserved) { const error = new Error('race'); error.name = 'TransactionCanceledException'; error.CancellationReasons = [{ Code: 'ConditionalCheckFailed' }]; throw error; }
      reserved += Number(values[':estimate'].N);
    },
  };
  const env = { FAST_GATE_TABLE: 'fast', REQUESTS_TABLE: 'requests' };
  const outcomes = await Promise.all([reserve(ddb, env, 'owner-1', 300, { request_id: { S: 'req-1' } }), reserve(ddb, env, 'owner-1', 300, { request_id: { S: 'req-2' } })]);
  assert.deepEqual(outcomes.sort(), [false, true]);
  assert.equal(reserved, 300);
});
test('reservation and pending outbox record commit atomically without stale reset', async () => {
  const calls = [];
  const ddb = {
    async getItem() { return { Item: { remaining_microcents: { N: '1000' }, reserved_microcents: { N: '0' }, openrouter_reserved_microcents: { N: '0' } } }; },
    async transactWriteItems(p) { calls.push(p); },
  };
  const outbox = { request_id: { S: 'req-1' }, state: { S: 'pending' } };
  assert.equal(await reserve(ddb, { FAST_GATE_TABLE: 'fast', REQUESTS_TABLE: 'requests' }, 'owner-1', 300, outbox), true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].TransactItems[0].Update.UpdateExpression.includes('openrouter_reserved_microcents'), true);
  assert.equal(calls[0].TransactItems[1].Put.Item.request_id.S, 'req-1');
});
test('definite upstream denial releases its own hold with outbox state atomically', async () => {
  const calls = [];
  await release({ async transactWriteItems(p) { calls.push(p); } }, { FAST_GATE_TABLE: 'fast', REQUESTS_TABLE: 'requests' }, 'owner-1', 300, 'req-1');
  assert.equal(calls.length, 1);
  assert.equal(calls[0].TransactItems[0].Update.UpdateExpression.includes('openrouter_reserved_microcents'), true);
  assert.equal(calls[0].TransactItems[1].Update.Key.request_id.S, 'req-1');
});
test('a negative pool (settlement overshoot) is exhausted: no reservation is written', async () => {
  for (const remaining of ['-1', '-900000000000000000000']) {
    let writes = 0;
    const ddb = {
      async getItem() { return { Item: { remaining_microcents: { N: remaining }, reserved_microcents: { N: '0' }, openrouter_reserved_microcents: { N: '0' } } }; },
      async transactWriteItems() { writes++; },
    };
    assert.equal(await reserve(ddb, { FAST_GATE_TABLE: 'fast', REQUESTS_TABLE: 'requests' }, 'owner-1', 1, { request_id: { S: 'req-1' } }), false);
    assert.equal(writes, 0);
  }
});
