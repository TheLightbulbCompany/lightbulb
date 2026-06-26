#!/usr/bin/env node
// atlas-activation-check.js
//
// Bootstrap validator. Checks env vars, durable-state defaults, security stack,
// ClawHavoc denylist completeness.
//
// Gateway-free: this tool reads only the local filesystem (openclaw.json) and an
// optional state-keys manifest (ATLAS_STATE_KEYS env, a JSON array of present
// `atlas/*` keys). It never calls the gateway tool proxy. The new state model creates
// keys lazily on first `state.set`, so absent keys are informational (not a
// blocker) unless a manifest is supplied to validate against.

const fs = require('fs');
const { CLAWHAVOC_DENYLIST } = require('./atlas-clawhavoc-enforcer.js');
const { STANDARD_SECURITY_STACK } = require('./atlas-skill-recommender.js');

function checkEnv() {
  const checks = [];
  const required = ['OPENCLAW_HOOK_TOKEN'];
  for (const v of required) {
    checks.push({
      check: `env_var_${v}`,
      pass: !!process.env[v],
      severity: 'blocker',
      reason: process.env[v] ? null : `${v} not set`,
      remediation: process.env[v] ? null : `Set ${v} in your environment.`
    });
  }
  return checks;
}

function checkFastIoDefaults() {
  // Durable state lives in native `atlas/*` state keys (state.get/state.set),
  // not the old fast-io tool. These keys are created lazily on first write, so
  // their absence at bootstrap is NOT a blocker. When ATLAS_STATE_KEYS is
  // supplied (a JSON array of present keys), validate presence as info; with no
  // manifest, report info-pass with the lazy-init note. No gateway read.
  const expectedKeys = [
    'atlas/intakes',
    'atlas/research-briefs',
    'atlas/requirement-matrix',
    'atlas/decisions',
    'atlas/notifications',
    'atlas/audit-log',
    'atlas/citation-tracking',
    'atlas/cookbook-refresh-state',
    'atlas/cookbook-docs',
    'atlas/cookbook-agent-snapshots',
    'atlas/failure-cases',
    'atlas/vet-progress',
    'atlas/recaps',
    'atlas/cost-estimates'
  ];
  let present = null;
  try {
    const parsed = JSON.parse(process.env.ATLAS_STATE_KEYS || 'null');
    if (Array.isArray(parsed)) present = new Set(parsed);
  } catch (e) { present = null; }

  const checks = [];
  for (const key of expectedKeys) {
    const known = present ? present.has(key) : null;
    checks.push({
      check: `state_default_${key}`,
      // `info` severity, never `blocker` — a missing key self-heals on first
      // state.set, so it must not fail the activation gate.
      pass: known === false ? false : true,
      severity: 'info',
      reason: known === false ? `${key} not yet initialized (created lazily on first write)` : null,
      remediation: known === false ? `Optional: pre-seed ${key} via BOOTSTRAP.md, or let it initialize on first use.` : null
    });
  }
  return checks;
}

function checkOpenclawJson() {
  const checks = [];
  try {
    const cfg = JSON.parse(fs.readFileSync('openclaw.json', 'utf8'));
    const denyList = (cfg.tools && cfg.tools.deny) || [];
    const missing = CLAWHAVOC_DENYLIST.filter(d => !denyList.includes(d));
    checks.push({
      check: 'clawhavoc_denylist_complete',
      pass: missing.length === 0,
      severity: 'blocker',
      reason: missing.length === 0 ? null : `tools.deny missing: ${missing.join(', ')}`,
      remediation: `Add to openclaw.json tools.deny array: ${missing.join(', ')}`
    });
    const skills = (cfg.agents && cfg.agents.list && cfg.agents.list[0] && cfg.agents.list[0].skills) || [];
    const missingSkills = STANDARD_SECURITY_STACK.filter(s => !skills.includes(s));
    checks.push({
      check: 'standard_security_stack_present',
      pass: missingSkills.length === 0,
      severity: 'blocker',
      reason: missingSkills.length === 0 ? null : `agents.list[0].skills missing: ${missingSkills.join(', ')}`,
      remediation: `Add to openclaw.json skills array: ${missingSkills.join(', ')}`
    });
  } catch (e) {
    checks.push({
      check: 'openclaw_json_present',
      pass: false,
      severity: 'blocker',
      reason: `openclaw.json not readable: ${e.message}`,
      remediation: 'Verify openclaw.json exists in agent dir and is valid JSON.'
    });
  }
  return checks;
}

function main() {
  const checks = [
    ...checkEnv(),
    ...checkFastIoDefaults(),
    ...checkOpenclawJson()
  ];
  const blockers = checks.filter(c => c.severity === 'blocker' && !c.pass);
  process.stdout.write(JSON.stringify({
    pass: blockers.length === 0,
    checks: checks,
    blocker_count: blockers.length,
    summary: blockers.length === 0 ? 'Atlas activation check PASSED. Ready for use.' : `Atlas activation check FAILED: ${blockers.length} blocker(s). See checks for remediation.`
  }));
  process.exit(blockers.length === 0 ? 0 : 1);
}

if (require.main === module) main();

module.exports = { checkEnv, checkFastIoDefaults, checkOpenclawJson };
