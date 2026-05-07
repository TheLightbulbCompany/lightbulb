#!/usr/bin/env node
// atlas-cookbook-reader.js
//
// Reads from two-source cookbook (OpenClaw docs cache + agent snapshots).
// Applies frozen source-preference table per D2.

const fs = require('fs');
const path = require('path');
const os = require('os');
const { execSync } = require('child_process');
const https = require('https');

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
const FAST_IO_DOCS_KEY_PREFIX = 'atlas-cookbook/openclaw-docs/';
const FAST_IO_AGENTS_KEY_PREFIX = 'atlas-cookbook/agent-snapshots/';

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
    https.get(url, { timeout: 10000 }, (res) => {
      if (res.statusCode !== 200) { resolve({ status: res.statusCode, body: null }); return; }
      let body = '';
      res.on('data', (c) => body += c);
      res.on('end', () => resolve({ status: 200, body }));
    }).on('error', (e) => resolve({ status: 0, body: null, error: e.message }));
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
    const cacheState = execSync(
      `openclaw.invoke --tool fast-io --action read --args-json '{"key":"atlas-config/cookbook-refresh-state"}'`,
      { encoding: 'utf8', stdio: ['pipe', 'pipe', 'ignore'] }
    );
    const parsed = JSON.parse(cacheState);
    cachedUrls = new Set(parsed.indexed_urls || []);
  } catch (e) { /* first run */ }

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

async function fetchPages(urls, maxFetches) {
  const fetched = [];
  for (const url of urls.slice(0, maxFetches)) {
    const result = await fetchUrl(url);
    if (result.status === 200) {
      fetched.push({ url, body: result.body, fetched_at: new Date().toISOString() });
    } else {
      fetched.push({ url, error: result.status, fetched_at: new Date().toISOString() });
    }
  }
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
        files[f] = fs.readFileSync(path.join(agentPath, f), 'utf8');
      }
    }
    agents.push({ agent_name: dirName, files: files, snapshotted_at: new Date().toISOString() });
  }
  return { count: agents.length, agents: agents };
}

function readForBrief(intake) {
  return {
    sources: {
      openclaw_docs: { fast_io_key_prefix: FAST_IO_DOCS_KEY_PREFIX, count: 'see cookbook-refresh-state' },
      agent_snapshots: { fast_io_key_prefix: FAST_IO_AGENTS_KEY_PREFIX, count: 'see snapshot-agents' }
    },
    source_preference: COOKBOOK_SOURCE_PREFERENCE,
    intake_archetype: intake.proposed_archetype || 'unknown',
    note: 'Compress via summarize CLI before passing to compile-research-brief llm-task.'
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
