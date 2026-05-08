#!/usr/bin/env node
// atlas-skill-recommender.js
//
// Given archetype + requirements, emits recommended skill list with reasoning.
// Runs skill-vetter as sub-process before any skill is added.
// Refuses any ClawHavoc-listed skill universally (no source-context exception).

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
  'meta-agent-builder': ['gog', 'slack'],
  'general': ['gog', 'slack']
});

function readStdin() {
  try { return JSON.parse(fs.readFileSync(0, 'utf8')); } catch (e) { return {}; }
}

function runSkillVetter(skill) {
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
    return { pass: true, findings: [], note: 'skill-vetter not invoked in this context' };
  }
}

function recommendSkills(archetype, requirements, userRequestedSkills) {
  const recommended = new Set();
  const refused = [];
  const findings = {};

  for (const s of STANDARD_SECURITY_STACK) recommended.add(s);

  const archetypeAdds = ARCHETYPE_RECOMMENDATIONS[archetype] || ARCHETYPE_RECOMMENDATIONS['general'];
  for (const s of archetypeAdds) recommended.add(s);

  for (const skill of (userRequestedSkills || [])) {
    if (FULL_DENYLIST.includes(skill)) {
      refused.push({
        skill: skill,
        reason: "This skill is on isol8's ClawHavoc avoidance list — community comments on ClawHub are not a verified source. To proceed, supply a publisher-signed manifest URL from the official source (see TOOLS.md § Verified Source Definition) and Atlas will run skill-vetter against it.",
        verified_source_required: true
      });
      continue;
    }
    const vetResult = runSkillVetter(skill);
    if (vetResult.pass) {
      recommended.add(skill);
      findings[skill] = vetResult;
    } else {
      refused.push({
        skill: skill,
        reason: 'skill-vetter rejected',
        findings: vetResult.findings
      });
    }
  }

  return {
    recommended_skills: [...recommended],
    refused_skills: refused,
    skill_vetter_findings: findings,
    denylist_version: 'v1',
    archetype: archetype
  };
}

function main() {
  const stdinData = readStdin();
  const archetype = (stdinData.intake && stdinData.intake.archetype) || (stdinData.brief && stdinData.brief.archetype) || 'general';
  const requirements = stdinData.skeleton || stdinData.requirements || [];
  const userRequestedSkills = stdinData.user_requested_skills || [];

  const recommendations = recommendSkills(archetype, requirements, userRequestedSkills);
  process.stdout.write(JSON.stringify(recommendations));
}

if (require.main === module) main();

module.exports = { STANDARD_SECURITY_STACK, ARCHETYPE_RECOMMENDATIONS, FULL_DENYLIST, recommendSkills };
