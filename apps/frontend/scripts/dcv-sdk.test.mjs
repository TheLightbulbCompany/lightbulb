import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { JSDOM } from 'jsdom';

// Execute the shipped SDK, not a mock viewer. Reintroducing DCV's broad native
// IPC detection must fail this test before any session or AWS request is needed.
test('DCV loads in Tauri without using its IPC or requesting local media', async () => {
  const source = await readFile(new URL('../public/nice-dcv-web-client-sdk/dcvjs-esm/dcv.js', import.meta.url), 'utf8');
  const dom = new JSDOM('', { url: 'https://flickmeon.ai', runScripts: 'outside-only' });
  const { window } = dom;
  window.TextDecoder = TextDecoder;
  window.TextEncoder = TextEncoder;
  const messages = [];
  let captureRequests = 0;
  window.__TAURI_INTERNALS__ = {};
  window.webkit = { messageHandlers: { ipc: { postMessage: message => messages.push(message) } } };
  // No EventTarget methods: the deployed missing-media shim supplied {}.
  // Resolve capture only to keep the unpatched SDK's extra rejection from
  // obscuring the synchronous startup error this regression covers.
  Object.defineProperty(window.navigator, 'mediaDevices', { value: {
    getUserMedia: async () => { captureRequests++; return { id: 'test' }; },
  }, configurable: true });
  try {
    window.eval(source.replace(/export\{(\w+) as default\};?\s*$/, 'globalThis.dcv = $1;'));
    assert.equal(typeof window.dcv.authenticate, 'function');
    assert.equal(captureRequests, 0, 'a remote viewer must not request local microphone access on import');
    assert.deepEqual(messages, [], 'DCV must not send native-redirection protocol messages to Tauri IPC');
  } finally {
    window.close();
  }
});
