#!/usr/bin/env node
// atlas-citation-verifier.js
//
// Architecture addition #3: quarterly cron pass on every external citation in Atlas's
// AGENTS.md and any Atlas-generated agents' AGENTS.md. On 2 consecutive 403/404/changed-content
// responses, surface stale-citation warning to user via Slack.
//
// Citation rot becomes a known surface with a watchdog, not a reason to skip citing.
//
// Modes:
//   --action=enumerate-agents (list all AGENTS.md paths)
//   --action=extract-citations (extract URLs/arxiv IDs from each AGENTS.md)
//   --action=verify (HTTP fetch each, record status; --quarterly flag for cron)
//   --action=update-tracking (apply 2-strike rule; emit stale_count + summary)

const fs = require('fs');
const path = require('path');
const os = require('os');
const https = require('https');
const { execSync } = require('child_process');

const CONSECUTIVE_FAILURE_THRESHOLD = 2; // 2 strikes → stale-citation warning per architecture addition #3
const FETCH_TIMEOUT_MS = 15000;

function parseArgs(argv) {
  const args = { action: 'verify', quarterly: false };
  for (const a of argv.slice(2)) {
    if (a.startsWith('--action=')) args.action = a.split('=')[1];
    else if (a === '--quarterly') args.quarterly = true;
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

  // URLs (http/https)
  const urlRegex = /https?:\/\/[^\s)\]]+/g;
  let m;
  while ((m = urlRegex.exec(content)) !== null) {
    citations.push({ type: 'url', value: m[0].replace(/[.,;:!?)]+$/, ''), file: filepath });
  }
  // arxiv IDs
  const arxivRegex = /arxiv\s+(\d{4}\.\d{4,5})/gi;
  while ((m = arxivRegex.exec(content)) !== null) {
    citations.push({ type: 'arxiv', value: m[1], url: `https://arxiv.org/abs/${m[1]}`, file: filepath });
  }
  // Deduplicate
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

function fastIoRead(key) {
  try {
    const result = execSync(
      `openclaw.invoke --tool fast-io --action read --args-json '{"key":"${key}"}'`,
      { encoding: 'utf8', stdio: ['pipe', 'pipe', 'ignore'] }
    );
    return JSON.parse(result);
  } catch (e) { return null; }
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

function updateTracking(verifyResults) {
  // 2-strike rule per architecture addition #3
  const tracking = fastIoRead('atlas-config/citation-tracking') || { citations: {} };
  const stale = [];
  for (const r of (verifyResults.results || [])) {
    const key = `${r.type}:${r.value}`;
    const prior = tracking.citations[key] || { consecutive_failures: 0, last_checked: null };
    if (r.passed) {
      tracking.citations[key] = { consecutive_failures: 0, last_checked: r.checked_at, last_status: r.status };
    } else {
      tracking.citations[key] = {
        consecutive_failures: prior.consecutive_failures + 1,
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
  fastIoWrite('atlas-config/citation-tracking', tracking);
  return {
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
    result = updateTracking(stdin);
  } else {
    result = { error: `unknown action: ${args.action}` };
  }

  process.stdout.write(JSON.stringify(result));
}

if (require.main === module) main().catch(e => { console.error(e); process.exit(1); });

module.exports = { CONSECUTIVE_FAILURE_THRESHOLD, extractCitationsFromAgentsMd, verifyCitations, updateTracking };
