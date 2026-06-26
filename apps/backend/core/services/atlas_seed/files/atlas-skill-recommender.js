#!/usr/bin/env node
// atlas-skill-recommender.js
//
// Given archetype + requirements, emits recommended skill list with reasoning.
// Refuses any ClawHavoc-listed skill universally (no source-context exception).
//
// Gateway-free: this tool is a pure transform reading its input on stdin. The
// hard ClawHavoc-denylist refusal is deterministic and stays inline. The
// skill-vetter audit (an external, gateway-bound tool the embedded runner can't
// invoke) is DEFERRED to the agent loop — non-denylist user skills land in
// `pending_vetter`, which the loop runs out of band and surfaces. No gateway tool calls.

const fs = require('fs');
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
  // Gateway-free: skill-vetter is an external audited tool the embedded runner
  // can't invoke. Defer the audit to the agent loop (it runs skill-vetter out of
  // band and surfaces findings). The deterministic denylist refusal below is the
  // hard, inline guard; this soft per-skill audit is always deferred here.
  return { pass: true, findings: [], deferred: true, note: 'skill-vetter audit deferred to agent loop (gateway-free runner)' };
}

function recommendSkills(archetype, requirements, userRequestedSkills) {
  const recommended = new Set();
  const refused = [];
  const findings = {};
  const pendingVetter = [];

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
    recommended.add(skill);
    findings[skill] = vetResult;
    if (vetResult.deferred) pendingVetter.push(skill);
  }

  return {
    recommended_skills: [...recommended],
    refused_skills: refused,
    skill_vetter_findings: findings,
    pending_vetter: pendingVetter,
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
