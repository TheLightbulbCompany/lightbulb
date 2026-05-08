#!/usr/bin/env node
// atlas-stack-overlap.js
//
// Reads ~/.openclaw/agents/*/openclaw.json structured fields only (per B4 — never AGENTS.md prose).
// Computes capability overlap against the proposed agent. Returns recommendation enum:
// extend / separate / no_overlap / ambiguous (4 values per B4 correction).
//
// Per A6: if structured fields are absent in any prior-agent openclaw.json, returns
// "insufficient structured data" and routes to llm-task fallback per B5. NEVER silent
// regress to prose parsing.

const fs = require('fs');
const path = require('path');
const os = require('os');

// Thresholds tagged "v1 default, revisit after 5 user builds" per M2 calibration pattern
const THRESHOLDS = Object.freeze({
  EXTEND_FLOOR: 70, // overlap >= 70% AND archetype keyword match
  SEPARATE_CEILING: 30, // overlap < 30% → separate (or no_overlap if 0)
  AMBIGUOUS_BAND: [30, 70] // ambiguous range, routes to llm-task fallback
});

function readStdin() {
  try { return JSON.parse(fs.readFileSync(0, 'utf8')); } catch (e) { return {}; }
}

function readAgentRegistry() {
  const agentsDir = path.join(os.homedir(), '.openclaw', 'agents');
  if (!fs.existsSync(agentsDir)) return [];
  const entries = [];
  for (const dirName of fs.readdirSync(agentsDir)) {
    if (dirName.startsWith('.')) continue; // skip staging dirs
    const cfgPath = path.join(agentsDir, dirName, 'openclaw.json');
    if (!fs.existsSync(cfgPath)) continue;
    try {
      const cfg = JSON.parse(fs.readFileSync(cfgPath, 'utf8'));
      const agent = (cfg.agents && cfg.agents.list && cfg.agents.list[0]) || {};
      entries.push({
        agent_name: dirName,
        skills: Array.isArray(agent.skills) ? agent.skills : [],
        archetype: agent.archetype || null, // structured archetype tag (added per B4 to Atlas-generated agents)
        description: typeof agent.description === 'string' ? agent.description : null,
        has_structured_fields: !!(agent.archetype || agent.description) && Array.isArray(agent.skills)
      });
    } catch (e) { /* skip malformed */ }
  }
  return entries;
}

function computeOverlap(proposedSkills, proposedArchetype, existingAgents) {
  const proposedSet = new Set(proposedSkills);
  const conflicts = [];
  let maxOverlap = 0;

  for (const agent of existingAgents) {
    const existingSet = new Set(agent.skills);
    const intersection = [...proposedSet].filter(s => existingSet.has(s));
    const union = new Set([...proposedSet, ...existingSet]);
    const jaccard = union.size === 0 ? 0 : (intersection.length / union.size) * 100;
    const archetypeMatch = proposedArchetype && agent.archetype && proposedArchetype === agent.archetype;
    if (jaccard > 5) {
      conflicts.push({
        agent_name: agent.agent_name,
        overlap_score: Math.round(jaccard),
        archetype_match: archetypeMatch,
        shared_skills: intersection
      });
    }
    if (jaccard > maxOverlap) maxOverlap = jaccard;
  }
  return { max_overlap: Math.round(maxOverlap), conflicts };
}

function classify(maxOverlap, conflicts) {
  if (maxOverlap === 0) return 'no_overlap';
  if (maxOverlap >= THRESHOLDS.EXTEND_FLOOR && conflicts.some(c => c.archetype_match)) return 'extend';
  if (maxOverlap < THRESHOLDS.SEPARATE_CEILING) return 'separate';
  return 'ambiguous'; // 30-70 band → routes to llm-task fallback per B5
}

function main() {
  const intake = readStdin();
  const proposedSkills = intake.proposed_skills || [];
  const proposedArchetype = intake.proposed_archetype || null;

  const existingAgents = readAgentRegistry();

  // A6 enforcement: if any agent in the registry lacks structured fields, refuse to silent-regress to prose parsing
  const insufficientStructure = existingAgents.filter(a => !a.has_structured_fields);
  if (insufficientStructure.length > 0 && existingAgents.length > 0) {
    process.stdout.write(JSON.stringify({
      pass: false,
      error: 'insufficient_structured_data',
      message: 'Some prior agents lack structured archetype/description fields in openclaw.json. Routing to llm-task fallback per B5.',
      agents_missing_structure: insufficientStructure.map(a => a.agent_name),
      route_to_llm_task: true,
      overlap_score: null,
      recommendation: 'ambiguous'
    }));
    return;
  }

  const { max_overlap, conflicts } = computeOverlap(proposedSkills, proposedArchetype, existingAgents);
  const recommendation = classify(max_overlap, conflicts);

  process.stdout.write(JSON.stringify({
    pass: true,
    overlap_score: max_overlap,
    conflicting_agents: conflicts,
    recommendation: recommendation,
    threshold_version: 'v1_default_revisit_after_5_builds'
  }));
}

if (require.main === module) main();

module.exports = { THRESHOLDS, readAgentRegistry, computeOverlap, classify };
