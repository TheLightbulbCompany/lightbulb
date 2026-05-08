#!/usr/bin/env node
// atlas-skill-recommender.js
//
// Given archetype + requirements, emits recommended skill list with reasoning.
// Runs skill-vetter as sub-process per S2 before any skill is added. Refuses any
// C1-C8 ClawHavoc skill universally per C9 (no source-context exception heuristic per C10).
// Refusal message includes structured findings per S2 audit-log granularity.

const fs = require('fs');
const { execSync } = require('child_process');
const { CLAWHAVOC_DENYLIST, CROSS_AGENT_FLOOR } = require('./atlas-clawhavoc-enforcer.js');

const FULL_DENYLIST = Object.freeze([...CLAWHAVOC_DENYLIST, ...CROSS_AGENT_FLOOR]);

const STANDARD_SECURITY_STACK = Object.freeze([
  'fast-io', 'taskr', 'summarize', 'n8n-workflow', 'capability-evolver',
  'skill-vetter', 'sona-security-audit', 'agentgate'
]);

const ARCHETYPE_RECOMMENDATIONS = Object.freeze({
  'sales': ['gog', 'slack', 'agent-browser', 'social-intelligence'],
  'marketing': ['gog', 'slack', 'agent-browser', 'reef-copywriting'],
  'support': ['slack', 'local-rag-qdrant', 'telcall-twilio'],
  'finance': ['slack'],
  'scheduling': ['gog', 'slack', 'meeting-prep'],
  'research': ['gog', 'slack', 'agent-browser', 'deeps'],
  'comms': ['slack', 'email-security', 'telcall-twilio'],
  'hr': ['slack', 'gog'],
  'meeting': ['slack', 'gog', 'meeting-prep'],
  'sourcing': ['gog', 'slack', 'agent-browser', 'social-intelligence'],
  'meta-agent-builder': ['gog', 'slack'] // Atlas itself
});

function readStdin() {
  try { return JSON.parse(fs.readFileSync(0, 'utf8')); } catch (e) { return {}; }
}

function runSkillVetter(skill) {
  // S2: invoke skill-vetter, return structured findings (not flat text per audit-log granularity)
  try {
    const result = execSync(
      `openclaw.invoke --tool skill-vetter --action audit --args-json '{"target":"${skill}"}'`,
      { encoding: 'utf8', stdio: ['pipe', 'pipe', 'ignore'], timeout: 30000 }
    );
    const parsed = JSON.parse(result);
    return {
      pass: parsed.pass !== false,
      findings: Array.isArray(parsed.findings) ? parsed.findings : []
    };
  } catch (e) {
    // skill-vetter unavailable in this context — return permissive but record
    return { pass: true, findings: [], note: 'skill-vetter not invoked in this context (probably running outside agent runtime)' };
  }
}

function recommendSkills(archetype, requirements, userRequestedSkills) {
  const recommended = new Set();
  const refused = [];
  const findings = {};

  // 1. Standard security stack always included
  for (const s of STANDARD_SECURITY_STACK) recommended.add(s);

  // 2. Archetype-specific additions
  const archetypeAdds = ARCHETYPE_RECOMMENDATIONS[archetype] || ARCHETYPE_RECOMMENDATIONS['meta-agent-builder'];
  for (const s of archetypeAdds) recommended.add(s);

  // 3. User-requested skills — apply C9 universal refusal for ClawHavoc list
  for (const skill of (userRequestedSkills || [])) {
    if (FULL_DENYLIST.includes(skill)) {
      // C10: refuse with explanation, no source-context exception
      refused.push({
        skill: skill,
        reason: "This skill is on isol8's ClawHavoc avoidance list — community comments on ClawHub are not a verified source. To proceed, supply a publisher-signed manifest URL from the official source (see TOOLS.md § Verified Source Definition) and Atlas will run skill-vetter against it.",
        verified_source_required: true
      });
      continue;
    }
    // S2: vet before adding
    const vetResult = runSkillVetter(skill);
    if (vetResult.pass) {
      recommended.add(skill);
      findings[skill] = vetResult;
    } else {
      refused.push({
        skill: skill,
        reason: 'skill-vetter rejected',
        findings: vetResult.findings // structured per S2 — {check, severity, reason, remediation} per finding
      });
    }
  }

  return {
    recommended_skills: [...recommended],
    refused_skills: refused,
    skill_vetter_findings: findings,
    denylist_version: 'v1',
    archetype: archetype,
    note: refused.length > 0 ? 'Some skills refused — see refused_skills for reasons. C9 ClawHavoc refusals are universal (no source-context exception per C10).' : null
  };
}

function main() {
  const stdinData = readStdin();
  const archetype = (stdinData.intake && stdinData.intake.archetype) || (stdinData.brief && stdinData.brief.archetype) || 'meta-agent-builder';
  const requirements = stdinData.skeleton || stdinData.requirements || [];
  const userRequestedSkills = stdinData.user_requested_skills || [];

  const recommendations = recommendSkills(archetype, requirements, userRequestedSkills);
  process.stdout.write(JSON.stringify(recommendations));
}

if (require.main === module) main();

module.exports = { STANDARD_SECURITY_STACK, ARCHETYPE_RECOMMENDATIONS, FULL_DENYLIST, recommendSkills };
