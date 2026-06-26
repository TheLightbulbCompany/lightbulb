#!/usr/bin/env node
// atlas-listing-builder.js
//
// Step 7: zero-LLM deterministic extraction from already-vetted artifacts.
// Cross-referenced from A10. Architectural property: can't drift from vetted state.
//
// Gateway-free: this tool is a pure transform. The build's vetted artifacts arrive
// via env vars — MATRIX (`state.get atlas/requirement-matrix` map[build_id]),
// RECAP (`atlas/recaps` map[build_id]), INTAKE (`atlas/intakes` map[build_id]) —
// and the continuity S10 bundle hash via S10_HASH. The deployed openclaw.json,
// AGENTS.md, and agent-dir bundle hash are read from the local filesystem
// (genuine fs, not gateway state). No gateway tool calls.

const fs = require('fs');
const path = require('path');
const os = require('os');
const crypto = require('crypto');

function parseArgs(argv) {
  const args = { action: 'build', buildId: null, agentName: null, currentHash: null };
  for (const a of argv.slice(2)) {
    if (a.startsWith('--action=')) args.action = a.split('=')[1];
    else if (a.startsWith('--build-id=')) args.buildId = a.split('=')[1];
    else if (a.startsWith('--agent-name=')) args.agentName = a.split('=')[1];
    else if (a.startsWith('--current-hash=')) args.currentHash = a.split('=')[1];
  }
  return args;
}

function readEnvJson(name) {
  try { return JSON.parse(process.env[name] || 'null'); } catch (e) { return null; }
}

function buildListing(buildId, agentName) {
  const matrix = readEnvJson('MATRIX');
  const recap = readEnvJson('RECAP');
  const intake = readEnvJson('INTAKE');
  const agentDir = path.join(os.homedir(), '.openclaw', 'agents', agentName);
  let openclawJson = null;
  try {
    openclawJson = JSON.parse(fs.readFileSync(path.join(agentDir, 'openclaw.json'), 'utf8'));
  } catch (e) { /* fallback to staging */ }

  if (!matrix || !openclawJson) {
    return { error: 'missing artifacts — listing cannot be built without vetted matrix and deployed openclaw.json' };
  }

  const recapCounts = (recap && recap.counts) || {};
  const skills = (openclawJson.agents && openclawJson.agents.list && openclawJson.agents.list[0] && openclawJson.agents.list[0].skills) || [];

  let neverDoes = [];
  try {
    const agentsMd = fs.readFileSync(path.join(agentDir, 'AGENTS.md'), 'utf8');
    const m = agentsMd.match(/##\s+What\s+\[?[^\]]+\]?\s+Never\s+Does[\s\S]+?(?=\n##\s|$)/);
    if (m) {
      neverDoes = m[0].split('\n').filter(l => l.startsWith('- ')).map(l => l.slice(2));
    }
  } catch (e) { /* AGENTS.md not present */ }

  const gates = ['architecture_signoff_S9', 'file_write_S10', 'marketplace_submit_S11'];
  if (skills.includes('agentgate')) gates.push('security_exception_A14');

  return {
    name: agentName,
    archetype: (intake && (intake.proposed_archetype || intake.archetype)) || (openclawJson.agents && openclawJson.agents.list && openclawJson.agents.list[0] && openclawJson.agents.list[0].archetype) || 'unknown',
    requirements_covered_count: recapCounts.no_change || 0,
    requirements_partial_count: recapCounts.changed || 0,
    requirements_uncovered_count: recapCounts.dropped || recapCounts.unvetted || 0,
    vet_completion_status: recapCounts.unvetted === 0 ? 'complete' : 'partial',
    security_stack: skills.filter(s => ['fast-io', 'taskr', 'summarize', 'n8n-workflow', 'capability-evolver', 'skill-vetter', 'sona-security-audit', 'agentgate'].includes(s)),
    agentgate_gates: gates,
    never_does: neverDoes,
    listed_at: new Date().toISOString(),
    note: 'Generated deterministically from vetted artifacts. Zero-LLM per P4.'
  };
}

function bundleHash(agentName) {
  const agentDir = path.join(os.homedir(), '.openclaw', 'agents', agentName);
  if (!fs.existsSync(agentDir)) return { error: `agent dir missing: ${agentDir}` };
  const hash = crypto.createHash('sha256');
  const files = fs.readdirSync(agentDir, { withFileTypes: true })
    .filter(d => d.isFile())
    .map(d => path.join(agentDir, d.name))
    .sort();
  for (const f of files) {
    hash.update(path.basename(f));
    hash.update(fs.readFileSync(f));
  }
  return { sha256: hash.digest('hex'), file_count: files.length, agent_dir: agentDir };
}

function continuityCheck(buildId, currentHash) {
  // The S10 build-time bundle hash arrives via S10_HASH (the caller pulls it
  // from the build_complete record in atlas/audit-log and passes it in).
  const s10Hash = (process.env.S10_HASH || '').trim() || null;
  if (!s10Hash) {
    return { continuity_holds: false, error: 'S10 hash not found in audit log; cannot verify continuity. HARD REFUSE per Marketplace Boundary Contract.' };
  }
  return {
    continuity_holds: s10Hash === currentHash,
    s10_hash: s10Hash,
    current_hash: currentHash,
    error: s10Hash === currentHash ? null : 'Bundle hash divergence between S10 and S11. HARD REFUSE per Marketplace Boundary Contract.'
  };
}

function main() {
  const args = parseArgs(process.argv);
  let result;
  if (args.action === 'bundle-hash') {
    if (!args.agentName) { console.error('--agent-name required'); process.exit(2); }
    result = bundleHash(args.agentName);
  } else if (args.action === 'continuity-check') {
    if (!args.buildId || !args.currentHash) { console.error('--build-id and --current-hash required'); process.exit(2); }
    result = continuityCheck(args.buildId, args.currentHash);
  } else {
    if (!args.buildId || !args.agentName) { console.error('--build-id and --agent-name required'); process.exit(2); }
    result = buildListing(args.buildId, args.agentName);
  }
  process.stdout.write(JSON.stringify(result));
}

if (require.main === module) main();

module.exports = { buildListing, bundleHash, continuityCheck };
