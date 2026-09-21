import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { abortOnClose } from './disconnect.mjs';

test('closing the client stream aborts the upstream request', () => {
  const stream = new EventEmitter();
  const signal = abortOnClose(stream);
  assert.equal(signal.aborted, false);
  stream.emit('close');
  assert.equal(signal.aborted, true);
});
