// Vendor-time smoke for the openclaw-host sidecar (run by vendor-sidecars.sh after pruning).
//   node smoke-node-host.mjs <node-binary> <openclaw-host-dir>
// Plays the gateway on loopback the way the backend broker does (node_broker.py), starts the
// vendored `openclaw node run` with the config shape the app writes (local_mcp.rs
// restrict_node_integrations) and a throwaway stdio MCP server, then drives each command
// family Hermes uses: system.which, system.run.prepare + system.run, file.fetch, dir.list and
// mcp.tools.call.v1. A prune that breaks startup or any of those paths fails the build here.
// Disposable HOME/state/config only; no network beyond 127.0.0.1.
import { spawn } from 'node:child_process';
import { mkdtempSync, realpathSync, rmSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomBytes, randomUUID } from 'node:crypto';

const [nodeBin, hostDir] = process.argv.slice(2);
if (!nodeBin || !hostDir) throw new Error('usage: smoke-node-host.mjs <node-binary> <openclaw-host-dir>');
const { WebSocketServer } = createRequire(join(hostDir, 'package.json'))('ws');
const REQUIRED = ['system.run', 'system.run.prepare', 'system.which', 'file.fetch', 'file.write',
  'dir.list', 'dir.fetch', 'mcp.tools.call.v1'];

const root = realpathSync(mkdtempSync(join(tmpdir(), 'lightbulb-node-smoke-')));
const state = join(root, 'state');
const config = join(root, 'openclaw.json');
writeFileSync(join(root, 'hello.txt'), 'lightbulb\n');
writeFileSync(join(root, 'mcp.mjs'), `
import { createInterface } from 'node:readline';
const send = v => process.stdout.write(JSON.stringify(v) + '\\n');
for await (const line of createInterface({ input: process.stdin })) {
  const r = JSON.parse(line);
  if (r.id === undefined) continue;
  const result = r.method === 'initialize'
    ? { protocolVersion: r.params.protocolVersion, capabilities: { tools: {} }, serverInfo: { name: 'smoke', version: '1' } }
    : r.method === 'tools/list' ? { tools: [{ name: 'echo', inputSchema: { type: 'object' } }] }
    : r.method === 'tools/call' ? { content: [{ type: 'text', text: 'smoke-' + r.params.arguments.v }] }
    : {};
  send({ jsonrpc: '2.0', id: r.id, result });
}`);
writeFileSync(config, JSON.stringify({
  tools: { exec: { security: 'allowlist', ask: 'always' } },
  plugins: { entries: {
    anthropic: { config: { sessionCatalog: { enabled: false } } },
    ollama: { config: { nodeInference: { enabled: false } } },
  } },
  browser: { enabled: false },
  nodeHost: { browserProxy: { enabled: false }, mcp: { servers: { smoke: { command: nodeBin, args: [join(root, 'mcp.mjs')] } } } },
}));

const fail = message => { throw new Error(`node-host smoke: ${message}`); };
let child;
const wss = new WebSocketServer({ host: '127.0.0.1', port: 0, maxPayload: 26_214_400 });
const timer = setTimeout(() => { console.error('node-host smoke: timed out'); cleanup(1); }, 90_000);
function cleanup(code) {
  clearTimeout(timer);
  try { child?.kill('SIGKILL'); } catch {}
  wss.close();
  rmSync(root, { recursive: true, force: true });
  process.exit(code);
}

wss.on('connection', ws => {
  const pending = new Map();
  let nodeId, catalog;
  let catalogReady;
  const catalogSeen = new Promise(r => { catalogReady = r; });
  const send = frame => ws.send(JSON.stringify(frame));
  const invoke = (command, params) => new Promise(resolve => {
    const id = randomUUID();
    pending.set(id, resolve);
    send({ type: 'event', event: 'node.invoke.request',
      payload: { id, nodeId, command, paramsJSON: JSON.stringify(params), timeoutMs: 20_000 } });
  });
  const ok = (command, result) => {
    if (!result.ok) fail(`${command} failed: ${JSON.stringify(result.error)}`);
    // Most results carry payloadJSON; the MCP call answers with a plain payload object.
    const payload = typeof result.payloadJSON === 'string' ? JSON.parse(result.payloadJSON) : result.payload;
    if (!payload || typeof payload !== 'object') fail(`${command} returned no payload: ${JSON.stringify(result)}`);
    return payload;
  };
  send({ type: 'event', event: 'connect.challenge', payload: { nonce: randomBytes(18).toString('base64url'), ts: Date.now() } });
  ws.on('message', async raw => {
    const frame = JSON.parse(raw);
    if (frame.type !== 'req') return;
    if (frame.method === 'connect') {
      const p = frame.params;
      if (p.client?.id !== 'node-host') fail(`client id ${p.client?.id}`);
      const missing = REQUIRED.filter(c => !p.commands?.includes(c));
      if (missing.length) fail(`commands not declared: ${missing.join(', ')}`);
      nodeId = p.device.id;
      send({ type: 'res', id: frame.id, ok: true, payload: {
        type: 'hello-ok', protocol: 4, server: { version: 'lightbulb-smoke/1', connId: 'smoke' },
        features: { methods: ['node.invoke.result', 'node.invoke.progress', 'node.event', 'node.pluginTools.update'],
          events: ['node.invoke.request', 'node.invoke.cancel', 'node.invoke.input', 'tick'] },
        snapshot: { presence: [], health: {}, stateVersion: { presence: 0, health: 0 }, uptimeMs: 0 },
        auth: { role: 'node', scopes: [] },
        policy: { maxPayload: 26_214_400, maxBufferedBytes: 52_428_800, tickIntervalMs: 15_000 },
      } });
      try { await drive(); console.log('==> node-host smoke passed'); cleanup(0); }
      catch (error) { console.error(error.message); cleanup(1); }
      return;
    }
    send({ type: 'res', id: frame.id, ok: true });
    if (frame.method === 'node.invoke.result') pending.get(frame.params.id)?.(frame.params);
    if (frame.method === 'node.pluginTools.update' && frame.params?.tools?.length) { catalog = frame.params; catalogReady(); }
  });

  async function drive() {
    ok('system.which', await invoke('system.which', { bins: ['sh'] }));
    const prepared = ok('system.run.prepare', await invoke('system.run.prepare', {
      command: ['/bin/echo', 'lightbulb-smoke'], cwd: root, agentId: 'smoke', sessionKey: 'smoke', runId: randomUUID(), timeoutMs: 10_000 }));
    const { plan } = prepared;
    const ran = ok('system.run', await invoke('system.run', {
      command: plan.argv, cwd: plan.cwd, agentId: 'smoke', sessionKey: 'smoke', runId: randomUUID(), timeoutMs: 10_000,
      systemRunPlan: plan, approvalDecision: 'allow-once' }));
    if (ran.exitCode !== 0 || !ran.stdout.includes('lightbulb-smoke')) fail(`system.run output ${JSON.stringify(ran)}`);
    for (const [command, path] of [['file.fetch', join(root, 'hello.txt')], ['dir.list', root]]) {
      const pre = ok(command, await invoke(command, { path, followSymlinks: false, preflightOnly: true }));
      if (pre.ok !== true) fail(`${command} preflight ${JSON.stringify(pre)}`);
    }
    await catalogSeen;
    const tool = catalog.tools.find(t => t.name?.includes('echo')) ?? fail(`MCP catalog ${JSON.stringify(catalog)}`);
    const called = ok('mcp.tools.call.v1', await invoke('mcp.tools.call.v1', { server: 'smoke', tool: 'echo', arguments: { v: 'ok' } }));
    if (!JSON.stringify(called).includes('smoke-ok')) fail(`MCP call ${JSON.stringify(called)} (tool ${JSON.stringify(tool)})`);
  }
});

wss.on('listening', () => {
  child = spawn(nodeBin, ['--disable-warning=ExperimentalWarning', join(hostDir, 'node_modules/openclaw/openclaw.mjs'),
    'node', 'run', '--host', '127.0.0.1', '--port', String(wss.address().port)], {
    cwd: root,
    env: { PATH: '/usr/bin:/bin:/usr/sbin:/sbin', HOME: root, TMPDIR: root, OPENCLAW_HOME: join(root, 'home'),
      OPENCLAW_STATE_DIR: state, OPENCLAW_CONFIG_PATH: config, OPENCLAW_OAUTH_DIR: join(state, 'credentials'),
      OPENCLAW_GATEWAY_TOKEN: 'smoke' },
    stdio: ['ignore', 'inherit', 'inherit'],
  });
  child.on('exit', code => { console.error(`node-host smoke: host exited (${code}) before passing`); cleanup(1); });
});
