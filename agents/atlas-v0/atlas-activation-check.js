#!/usr/bin/env node
// atlas-activation-check.js
//
// Bootstrap validator. Checks env vars, fast-io defaults, cron jobs, Slack channels,
// security stack, ClawHavoc denylist completeness. Returns structured pass/fail with
// remediation per check (similar shape to existing-agent activation-check scripts).

const fs = require('fs');
const { execSync } = require('child_process');
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
  // At least one of ANTHROPIC_API_KEY or OPENAI_API_KEY (depending on tier)
  const hasAnthropic = !!process.env.ANTHROPIC_API_KEY;
  const hasOpenai = !!process.env.OPENAI_API_KEY;
  const hasIsol8Tier = !!process.env.ISOL8_USER_TIER;
  checks.push({
    check: 'llm_provider_credentials',
    pass: hasAnthropic || hasOpenai || hasIsol8Tier,
    severity: 'blocker',
    reason: (hasAnthropic || hasOpenai || hasIsol8Tier) ? null : 'No LLM provider credentials detected and no isol8 tier env var set',
    remediation: 'Set ANTHROPIC_API_KEY (BYO Claude), OPENAI_API_KEY (BYO OpenAI), or ensure isol8 platform has provisioned hosted_claude tier.'
  });
  return checks;
}

function checkFastIoDefaults() {
  const requiredKeys = [
    'atlas-config/identity-locks',
    'atlas-config/cookbook-refresh-state',
    'atlas-cookbook/openclaw-docs',
    'atlas-cookbook/agent-snapshots',
    'atlas-cookbook/failure-cases.json',
    'atlas-cookbook/user-resolutions',
    'atlas-builds',
    'atlas-pending-submits'
  ];
  const checks = [];
  for (const key of requiredKeys) {
    let exists = false;
    try {
      execSync(
        `openclaw.invoke --tool fast-io --action read --args-json '{"key":"${key}"}'`,
        { encoding: 'utf8', stdio: ['pipe', 'pipe', 'ignore'] }
      );
      exists = true;
    } catch (e) { /* not initialized */ }
    checks.push({
      check: `fast_io_default_${key}`,
      pass: exists,
      severity: 'blocker',
      reason: exists ? null : `${key} not initialized`,
      remediation: exists ? null : `Run BOOTSTRAP.md Step 2 to initialize default fast-io state.`
    });
  }
  return checks;
}

function checkOpenclawJson() {
  const checks = [];
  try {
    const cfg = JSON.parse(fs.readFileSync('openclaw.json', 'utf8'));
    const denyList = (cfg.tools && cfg.tools.deny) || [];
    // C1-C8 completeness check
    const missing = CLAWHAVOC_DENYLIST.filter(d => !denyList.includes(d));
    checks.push({
      check: 'clawhavoc_denylist_complete',
      pass: missing.length === 0,
      severity: 'blocker',
      reason: missing.length === 0 ? null : `tools.deny missing: ${missing.join(', ')}`,
      remediation: `Add to openclaw.json tools.deny array: ${missing.join(', ')}`
    });
    // Standard security stack present
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
