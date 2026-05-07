#!/usr/bin/env node
// atlas-clawhavoc-enforcer.js
//
// Scans an agent bundle (openclaw.json + workspace .md + .lobster + .js) for any reference
// to the canonical ClawHavoc denylist. Used at build-time (W7, build.lobster) and publish-time
// (W8, publish.lobster) with asymmetric override semantics — see TOOLS.md § Asymmetric Override
// Semantics and § Marketplace Boundary Contract.
//
// Build-time hit: HARD BLOCK with override available via security-exception agentgate (A14).
// Publish-time hit: HARD BLOCK with NO override path (Marketplace Boundary Contract / C11).

const fs = require('fs');
const path = require('path');

// ============================================================================
// CANONICAL CLAWHAVOC DENYLIST — frozen per P2 enforcement requirement.
// Single-file update site. Adding/removing entries propagates to all enforcement
// points (recommender filter, build-time enforcer, publish-time enforcer).
// ============================================================================
const CLAWHAVOC_DENYLIST = Object.freeze([
  'salesforce-api',
  'pipedrive-api',
  'attio-api',
  'zoho-recruit',
  'zoho-crm',
  'gmail',
  'mailchimp',
  'fathom-api'
]);

// Cross-agent floor (also denied; not strictly ClawHavoc but on the standard deny list)
const CROSS_AGENT_FLOOR = Object.freeze([
  'tavily',
  'foxreach-io',
  'campaign-orchestrator',
  'phone-caller',
  'linkedin-automation'
]);

function parseArgs(argv) {
  const args = { mode: 'build', buildId: null, agentName: null };
  for (const a of argv.slice(2)) {
    if (a.startsWith('--mode=')) args.mode = a.split('=')[1];
    else if (a.startsWith('--build-id=')) args.buildId = a.split('=')[1];
    else if (a.startsWith('--agent-name=')) args.agentName = a.split('=')[1];
  }
  return args;
}

function scanFileForDenylist(filepath, denylist) {
  if (!fs.existsSync(filepath)) return [];
  const content = fs.readFileSync(filepath, 'utf8');
  const violations = [];
  for (const denied of denylist) {
    // Whole-token match — avoid false positives like "gmail" matching "ungmail"
    const regex = new RegExp(`(^|[^a-zA-Z0-9_-])${denied.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}([^a-zA-Z0-9_-]|$)`, 'g');
    if (regex.test(content)) {
      violations.push({ file: filepath, denied_skill: denied });
    }
  }
  return violations;
}

function scanBundle(bundlePath, denylist) {
  if (!fs.existsSync(bundlePath)) {
    return { error: `bundle path does not exist: ${bundlePath}` };
  }
  const violations = [];
  const files = fs.readdirSync(bundlePath, { withFileTypes: true })
    .filter(d => d.isFile())
    .map(d => path.join(bundlePath, d.name));
  for (const f of files) {
    if (/\.(json|md|lobster|js)$/.test(f)) {
      violations.push(...scanFileForDenylist(f, denylist));
    }
  }
  return { violations };
}

function main() {
  const args = parseArgs(process.argv);
  const fullDenylist = [...CLAWHAVOC_DENYLIST, ...CROSS_AGENT_FLOOR];

  let bundlePath;
  if (args.mode === 'build') {
    if (!args.buildId) { console.error('--build-id required for build mode'); process.exit(2); }
    // Build-time: scan staged artifacts in fast-io location (mirrored on disk for verification)
    bundlePath = process.env.HOME + `/.openclaw/agents/.atlas-staging-${args.buildId}`;
  } else if (args.mode === 'publish') {
    if (!args.agentName) { console.error('--agent-name required for publish mode'); process.exit(2); }
    // Publish-time: scan the deployed agent directory
    bundlePath = process.env.HOME + `/.openclaw/agents/${args.agentName}`;
  } else {
    console.error(`unknown mode: ${args.mode}`); process.exit(2);
  }

  const result = scanBundle(bundlePath, fullDenylist);
  if (result.error) {
    process.stdout.write(JSON.stringify({ pass: false, error: result.error, violations: [] }));
    process.exit(0); // Don't crash the pipeline; let lobster step decide on .pass
    return;
  }

  // Filter false positives: openclaw.json's tools.deny LISTING the denylist is expected and not a violation
  const trueViolations = result.violations.filter(v => {
    if (path.basename(v.file) !== 'openclaw.json') return true;
    // Check if the denied skill appears OUTSIDE the tools.deny array
    const content = fs.readFileSync(v.file, 'utf8');
    try {
      const json = JSON.parse(content);
      const denyList = (json.tools && json.tools.deny) || [];
      if (denyList.includes(v.denied_skill)) {
        // Check if it appears anywhere else in the file
        const stripped = JSON.stringify({ ...json, tools: { ...json.tools, deny: [] } });
        return stripped.includes(v.denied_skill);
      }
    } catch (e) { /* malformed json — keep as violation */ }
    return true;
  });

  const out = {
    pass: trueViolations.length === 0,
    mode: args.mode,
    bundle_path: bundlePath,
    violations: trueViolations,
    denylist_version: 'v1', // bump on any change to CLAWHAVOC_DENYLIST or CROSS_AGENT_FLOOR
    refusal_message: trueViolations.length === 0 ? null :
      "This skill is on isol8's ClawHavoc avoidance list — install from official source via skill-vetter, then re-attempt publish."
  };

  process.stdout.write(JSON.stringify(out));
}

if (require.main === module) main();

module.exports = { CLAWHAVOC_DENYLIST, CROSS_AGENT_FLOOR, scanBundle };
