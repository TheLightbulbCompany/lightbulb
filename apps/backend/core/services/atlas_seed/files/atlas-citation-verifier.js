#!/usr/bin/env node
// atlas-citation-verifier.js
//
// Quarterly cron pass on every external citation in Atlas's AGENTS.md and any
// Atlas-generated agents' AGENTS.md. On 2 consecutive 403/404/changed-content
// responses, surfaces a stale-citation warning.
//
// Gateway-free: this tool is a pure transform. The directory walk + AGENTS.md
// reads use the local filesystem (genuine fs, not gateway state). Prior
// consecutive-failure tracking arrives via the TRACKING env var (the calling
// .lobster does a `state.get atlas/citation-tracking` and passes it); the verify
// results arrive on stdin. The NEW tracking is returned on stdout under
// `.tracking` for the .lobster to persist via `state.set` (stale info rides
// alongside). No gateway tool calls.

const fs = require('fs');
const path = require('path');
const os = require('os');
const https = require('https');

const CONSECUTIVE_FAILURE_THRESHOLD = 2;
const FETCH_TIMEOUT_MS = 15000;

function parseArgs(argv) {
  const args = { action: 'verify' };
  for (const a of argv.slice(2)) {
    if (a.startsWith('--action=')) args.action = a.split('=')[1];
  }
  return args;
}

function readStdin() {
  try { return JSON.parse(fs.readFileSync(0, 'utf8')); } catch (e) { return {}; }
}

function enumerateAgents() {
  const agentsDir = path.join(os.homedir(), '.openclaw', 'agents');
  const agents = [];
  if (!fs.existsSync(agentsDir)) return { agent_count: 0, agents: [] };
  for (const dirName of fs.readdirSync(agentsDir)) {
    if (dirName.startsWith('.')) continue;
    const agentsMdPath = path.join(agentsDir, dirName, 'AGENTS.md');
    if (fs.existsSync(agentsMdPath)) {
      agents.push({ agent_name: dirName, agents_md_path: agentsMdPath });
    }
  }
  return { agent_count: agents.length, agents };
}

function extractCitationsFromAgentsMd(filepath) {
  const content = fs.readFileSync(filepath, 'utf8');
  const citations = [];

  const urlRegex = /https?:\/\/[^\s)\]]+/g;
  let m;
  while ((m = urlRegex.exec(content)) !== null) {
    citations.push({ type: 'url', value: m[0].replace(/[.,;:!?)]+$/, ''), file: filepath });
  }
  const arxivRegex = /arxiv\s+(\d{4}\.\d{4,5})/gi;
  while ((m = arxivRegex.exec(content)) !== null) {
    citations.push({ type: 'arxiv', value: m[1], url: `https://arxiv.org/abs/${m[1]}`, file: filepath });
  }
  const seen = new Set();
  return citations.filter(c => {
    const key = `${c.type}:${c.value}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function fetchUrlStatus(url) {
  return new Promise((resolve) => {
    const req = https.get(url, { timeout: FETCH_TIMEOUT_MS, headers: { 'User-Agent': 'atlas-citation-verifier/1.0' } }, (res) => {
      resolve({ url, status: res.statusCode, checked_at: new Date().toISOString() });
      res.resume();
    });
    req.on('error', (e) => resolve({ url, status: 0, error: e.message, checked_at: new Date().toISOString() }));
    req.on('timeout', () => { req.destroy(); resolve({ url, status: 0, error: 'timeout', checked_at: new Date().toISOString() }); });
  });
}

async function verifyCitations(citations) {
  const results = [];
  for (const c of citations) {
    const url = c.url || c.value;
    const result = await fetchUrlStatus(url);
    const passed = result.status === 200;
    results.push({ ...c, url, status: result.status, passed, checked_at: result.checked_at, error: result.error });
  }
  return {
    total_count: results.length,
    passed_count: results.filter(r => r.passed).length,
    failed_count: results.filter(r => !r.passed).length,
    results
  };
}

// Pure transform: given the verify results and the PRIOR tracking object (loaded
// from state by the caller), compute the new tracking + the stale list. The
// caller persists `.tracking` back to state.
function updateTracking(verifyResults, priorTracking) {
  const prior = (priorTracking && typeof priorTracking === 'object' && priorTracking.citations)
    ? priorTracking
    : { citations: {} };
  const tracking = { citations: { ...prior.citations } };
  const stale = [];
  for (const r of (verifyResults.results || [])) {
    const key = `${r.type}:${r.value}`;
    const before = tracking.citations[key] || { consecutive_failures: 0, last_checked: null };
    if (r.passed) {
      tracking.citations[key] = { consecutive_failures: 0, last_checked: r.checked_at, last_status: r.status };
    } else {
      tracking.citations[key] = {
        consecutive_failures: before.consecutive_failures + 1,
        last_checked: r.checked_at,
        last_status: r.status,
        last_error: r.error
      };
      if (tracking.citations[key].consecutive_failures >= CONSECUTIVE_FAILURE_THRESHOLD) {
        stale.push({
          citation: r,
          consecutive_failures: tracking.citations[key].consecutive_failures
        });
      }
    }
  }
  return {
    tracking,
    stale_count: stale.length,
    stale_summary: stale.map(s => `${s.citation.type}:${s.citation.value} (${s.consecutive_failures} consecutive failures, last status ${s.citation.status})`).join('; '),
    stale_details: stale
  };
}

async function main() {
  const args = parseArgs(process.argv);
  const stdin = readStdin();
  let result;

  if (args.action === 'enumerate-agents') {
    result = enumerateAgents();
  } else if (args.action === 'extract-citations') {
    const agents = stdin.agents || [];
    const allCitations = [];
    for (const a of agents) {
      allCitations.push(...extractCitationsFromAgentsMd(a.agents_md_path));
    }
    result = { citation_count: allCitations.length, citations: allCitations };
  } else if (args.action === 'verify') {
    const citations = stdin.citations || [];
    result = await verifyCitations(citations);
  } else if (args.action === 'update-tracking') {
    let priorTracking = null;
    try { priorTracking = JSON.parse(process.env.TRACKING || 'null'); } catch (e) { priorTracking = null; }
    result = updateTracking(stdin, priorTracking);
  } else {
    result = { error: `unknown action: ${args.action}` };
  }

  process.stdout.write(JSON.stringify(result));
}

if (require.main === module) main().catch(e => { console.error(e); process.exit(1); });

module.exports = { CONSECUTIVE_FAILURE_THRESHOLD, extractCitationsFromAgentsMd, verifyCitations, updateTracking };
