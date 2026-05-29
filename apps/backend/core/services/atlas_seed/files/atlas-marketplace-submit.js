#!/usr/bin/env node
// atlas-marketplace-submit.js
//
// STEP 7 STUB. Real marketplace API not yet available. This stub:
//   1. Writes payload to fast-io: atlas-pending-submits/{{timestamp}}
//   2. Writes local preview file at ~/.openclaw/atlas/preview-submits/{{timestamp}}.json
//
// TODO when real API ships:
//   - Auth (per-user token vs OAuth?)
//   - Bundle upload (multipart vs signed URL?)
//   - Listing metadata schema
//   - Status callback (webhook vs polling?)
//
// Plus one-time migration on real-API ship: scan atlas-pending-submits/* for
// accumulated stubs and surface "you have N pending submits from before the
// marketplace launched — review and submit?" Documented as launch-time obligation.

const fs = require('fs');
const path = require('path');
const os = require('os');
const { execSync } = require('child_process');

function parseArgs(argv) {
  const args = { buildId: null, agentName: null, promptPending: false };
  for (const a of argv.slice(2)) {
    if (a.startsWith('--build-id=')) args.buildId = a.split('=')[1];
    else if (a.startsWith('--agent-name=')) args.agentName = a.split('=')[1];
    else if (a === '--prompt-pending') args.promptPending = true;
  }
  return args;
}

function readStdin() {
  try { return JSON.parse(fs.readFileSync(0, 'utf8')); } catch (e) { return {}; }
}

function fastIoWrite(key, data) {
  try {
    execSync(
      `openclaw.invoke --tool fast-io --action write --args-json '${JSON.stringify({ key, data }).replace(/'/g, `'\\''`)}'`,
      { encoding: 'utf8', stdio: ['pipe', 'pipe', 'ignore'] }
    );
    return true;
  } catch (e) { return false; }
}

function submitStub(buildId, agentName, payload) {
  const timestamp = Date.now();
  const fastIoKey = `atlas-pending-submits/${timestamp}`;
  const previewDir = path.join(os.homedir(), '.openclaw', 'atlas', 'preview-submits');
  const previewFile = path.join(previewDir, `${timestamp}.json`);

  fastIoWrite(fastIoKey, {
    build_id: buildId,
    agent_name: agentName,
    payload: payload,
    queued_at: new Date(timestamp).toISOString(),
    status: 'pending_real_api',
    todo_when_real_api_ships: [
      'Auth (per-user token vs OAuth?)',
      'Bundle upload (multipart vs signed URL?)',
      'Listing metadata schema',
      'Status callback (webhook vs polling?)'
    ]
  });

  if (!fs.existsSync(previewDir)) fs.mkdirSync(previewDir, { recursive: true });
  fs.writeFileSync(previewFile, JSON.stringify({
    build_id: buildId,
    agent_name: agentName,
    payload: payload,
    queued_at: new Date(timestamp).toISOString()
  }, null, 2));

  return {
    submitted: false,
    stub: true,
    queued: true,
    fast_io_key: fastIoKey,
    preview_file_path: previewFile,
    user_message: `Marketplace submit queued at ${new Date(timestamp).toISOString()}. Real API not yet available; preview file at ${previewFile} shows exactly what would have been submitted.`
  };
}

function promptPending() {
  return {
    action: 'prompt_pending_submits',
    note: 'Real implementation reads atlas-pending-submits/* and posts to build_status_channel.',
    reminder_template: 'You have {{N}} pending marketplace submits awaiting the real API. Review preview files at ~/.openclaw/atlas/preview-submits/ or wait for the API to ship.'
  };
}

function main() {
  const args = parseArgs(process.argv);
  if (args.promptPending) {
    process.stdout.write(JSON.stringify(promptPending()));
    return;
  }
  if (!args.buildId || !args.agentName) { console.error('--build-id and --agent-name required'); process.exit(2); }
  const payload = readStdin();
  process.stdout.write(JSON.stringify(submitStub(args.buildId, args.agentName, payload)));
}

if (require.main === module) main();

module.exports = { submitStub, promptPending };
