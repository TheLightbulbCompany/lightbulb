#!/usr/bin/env node
// atlas-cookbook-reader.js
//
// Reads from two-source cookbook (OpenClaw docs cache + agent snapshots).
// Applies frozen source-preference table per D2.
//
// Gateway-free: the doc-index fetch + page fetches use https, and the agent
// snapshot walk uses the local filesystem (~/.openclaw/agents) — both genuine,
// not gateway state. The only state input (the prior refresh-state, used to diff
// the doc index) arrives via the REFRESH_STATE env var (the calling .lobster
// does `state.get atlas/cookbook-refresh-state` and passes it). No gateway tool calls.

const fs = require('fs');
const path = require('path');
const os = require('os');
const https = require('https');
const http = require('http');

// ============================================================================
// FROZEN COOKBOOK SOURCE PREFERENCE TABLE
// ============================================================================
const COOKBOOK_SOURCE_PREFERENCE = Object.freeze({
  skill_manifest_schemas:          'agent_snapshots',
  lobster_branching_error_syntax:  'agent_snapshots',
  openclaw_json_trust_boundaries:  'agent_snapshots',
  bootstrap_cron_webhook_patterns: 'agent_snapshots',
  lobster_basic_schema:            'openclaw_docs',
  llm_task_call_shape:             'openclaw_docs',
  openclaw_json_top_level:         'openclaw_docs',
  channels:                        'openclaw_docs',
  clawhub_community_skills:        'agent_snapshots'
});

const OPENCLAW_DOCS_INDEX_URL = 'https://docs.openclaw.ai/llms.txt';
// Per-file content cap for agent snapshots — the whole snapshot lives under one
// state key, so unbounded full content would exceed the state value size ceiling.
const SNAPSHOT_FILE_PREVIEW_BYTES = 8192;

function parseArgs(argv) {
  const args = { action: 'read', maxFetches: 50 };
  for (const a of argv.slice(2)) {
    if (a.startsWith('--action=')) args.action = a.split('=')[1];
    else if (a.startsWith('--max-fetches=')) args.maxFetches = parseInt(a.split('=')[1], 10);
    else if (a === '--refresh') args.action = 'refresh-all';
  }
  return args;
}

function readStdin() {
  try { return JSON.parse(fs.readFileSync(0, 'utf8')); } catch (e) { return {}; }
}

function fetchUrl(url) {
  return new Promise((resolve) => {
    // Pick the client by scheme — https.get throws "Protocol 'http:' not
    // supported" for http:// index URLs, which would silently never cache them.
    const client = url.startsWith('http://') ? http : https;
    const req = client.get(url, { timeout: 10000 }, (res) => {
      if (res.statusCode !== 200) { resolve({ status: res.statusCode, body: null }); return; }
      let body = '';
      res.on('data', (c) => body += c);
      res.on('end', () => resolve({ status: 200, body }));
    });
    req.on('error', (e) => resolve({ status: 0, body: null, error: e.message }));
    // The { timeout } option fires this event but does NOT destroy the request —
    // without this, a server that accepts the TCP connection but never responds
    // hangs fetchUrl forever, stalling the whole daily cookbook-refresh cron.
    req.on('timeout', () => { req.destroy(); resolve({ status: 0, body: null, error: 'timeout' }); });
  });
}

async function diffIndex() {
  const indexResult = await fetchUrl(OPENCLAW_DOCS_INDEX_URL);
  if (indexResult.status !== 200) {
    return { error: `failed to fetch index: ${indexResult.status}`, added_count: 0, changed_count: 0 };
  }
  const newUrls = new Set(indexResult.body.split('\n').filter(l => l.startsWith('https://')));
  let cachedUrls = new Set();
  try {
    const parsed = JSON.parse(process.env.REFRESH_STATE || 'null');
    if (parsed && Array.isArray(parsed.indexed_urls)) cachedUrls = new Set(parsed.indexed_urls);
  } catch (e) { /* first run / no prior state */ }

  const added = [...newUrls].filter(u => !cachedUrls.has(u));
  const removed = [...cachedUrls].filter(u => !newUrls.has(u));
  return {
    added: added,
    removed: removed,
    added_count: added.length,
    removed_count: removed.length,
    changed_count: added.length + removed.length,
    indexed_urls: [...newUrls]
  };
}

async function fetchPages(urls, maxFetches, concurrency = 8) {
  // Bounded-concurrency pool: serial await would be maxFetches × timeout
  // worst-case (50 × 10s = 500s), which could clip the daily cron on a cold
  // first run. Same pattern as atlas-citation-verifier.verifyCitations.
  const slice = urls.slice(0, maxFetches);
  const fetched = new Array(slice.length);
  const queue = [...slice.entries()];
  async function worker() {
    let entry;
    while ((entry = queue.shift()) !== undefined) {
      const [i, url] = entry;
      const result = await fetchUrl(url);
      fetched[i] = result.status === 200
        ? { url, body: result.body, fetched_at: new Date().toISOString() }
        : { url, error: result.status, fetched_at: new Date().toISOString() };
    }
  }
  await Promise.all(Array.from({ length: Math.min(concurrency, slice.length || 1) }, worker));
  return fetched;
}

function snapshotAgents() {
  const agentsDir = path.join(os.homedir(), '.openclaw', 'agents');
  if (!fs.existsSync(agentsDir)) return { count: 0, agents: [] };
  const agents = [];
  for (const dirName of fs.readdirSync(agentsDir)) {
    if (dirName.startsWith('.')) continue;
    const agentPath = path.join(agentsDir, dirName);
    if (!fs.statSync(agentPath).isDirectory()) continue;
    const files = {};
    for (const f of fs.readdirSync(agentPath)) {
      if (/\.(json|md|lobster|js)$/.test(f)) {
        // Per-file try/catch so one unreadable file (perms, binary, etc.) is
        // recorded as an error rather than killing the whole snapshot walk.
        try {
          // Cap per-file content to a preview — the whole snapshot is stored under
          // ONE state key (atlas/cookbook-agent-snapshots); full content of many
          // agents × files would blow the state value past its size ceiling. The
          // preview is reference context for the research brief, not a reproduction.
          const body = fs.readFileSync(path.join(agentPath, f), 'utf8');
          files[f] = body.length > SNAPSHOT_FILE_PREVIEW_BYTES
            ? { preview: body.slice(0, SNAPSHOT_FILE_PREVIEW_BYTES), truncated: true, full_bytes: body.length }
            : body;
        } catch (e) {
          files[f] = { error: e.message };
        }
      }
    }
    agents.push({ agent_name: dirName, files: files, snapshotted_at: new Date().toISOString() });
  }
  return { count: agents.length, agents: agents };
}

function readForBrief(intake) {
  return {
    sources: {
      // Native state keys written by cookbook-refresh.lobster (the agent loop
      // reads these via state.get — the old fast-io namespace is gone).
      openclaw_docs: { state_key: 'atlas/cookbook-docs', count: 'see atlas/cookbook-refresh-state' },
      agent_snapshots: { state_key: 'atlas/cookbook-agent-snapshots', count: 'see atlas/cookbook-agent-snapshots' }
    },
    source_preference: COOKBOOK_SOURCE_PREFERENCE,
    intake_archetype: intake.proposed_archetype || 'unknown',
    note: 'The agent loop compresses these sources when synthesizing the research brief.'
  };
}

async function main() {
  const args = parseArgs(process.argv);
  const stdinData = readStdin();

  let result;
  if (args.action === 'diff-index') {
    result = await diffIndex();
  } else if (args.action === 'fetch-pages') {
    result = { fetched: await fetchPages(stdinData.added || [], args.maxFetches) };
  } else if (args.action === 'update-cache') {
    result = { write_count: (stdinData.fetched || []).length, mode: 'reports-writes' };
  } else if (args.action === 'snapshot-agents') {
    result = snapshotAgents();
  } else if (args.action === 'refresh-all') {
    const idx = await diffIndex();
    const fetched = await fetchPages(idx.added || [], 50);
    const snap = snapshotAgents();
    result = { diff: idx, fetched_count: fetched.length, snapshot_count: snap.count };
  } else {
    result = readForBrief(stdinData);
  }

  process.stdout.write(JSON.stringify(result));
}

if (require.main === module) main().catch(e => { console.error(e); process.exit(1); });

module.exports = { COOKBOOK_SOURCE_PREFERENCE, snapshotAgents };
