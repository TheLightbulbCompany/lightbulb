// Disposable stdio MCP fixture; no dependencies, credentials or external I/O.
import { createInterface } from 'node:readline';
let removed = false;
const send = value => process.stdout.write(JSON.stringify(value) + '\n');
for await (const line of createInterface({ input: process.stdin })) {
  const request = JSON.parse(line);
  if (request.id === undefined) continue;
  let result;
  if (request.method === 'initialize') result = {
    protocolVersion: request.params.protocolVersion,
    capabilities: { tools: { listChanged: true } }, serverInfo: { name: 'isolated-fixture', version: '1' },
  };
  else if (request.method === 'tools/list') result = { tools: removed ? [] : [
    { name: 'echo', description: 'Untrusted fixture metadata', inputSchema: { type: 'object', properties: { value: {}, remove: { type: 'boolean' } } } },
  ] };
  else if (request.method === 'tools/call') {
    result = { content: [{ type: 'text', text: JSON.stringify(request.params.arguments) }] };
    if (request.params.arguments.remove) {
      removed = true;
      setTimeout(() => send({ jsonrpc: '2.0', method: 'notifications/tools/list_changed' }), 50);
    }
  } else if (request.method === 'ping') result = {};
  else { send({ jsonrpc: '2.0', id: request.id, error: { code: -32601, message: 'unsupported' } }); continue; }
  send({ jsonrpc: '2.0', id: request.id, result });
}
