#!/usr/bin/env node
// atlas-det-llm-classifier.js
//
// Per-requirement classification: deterministic / LLM / hybrid with rationale.
// Flags warnings when generated agent's projected LLM ratio exceeds 15%.

const fs = require('fs');

const LLM_RATIO_WARN_THRESHOLD = 0.15;
const VERSION_TAG = 'v1_default_revisit_after_5_builds';

const DETERMINISTIC_INDICATORS = [
  /scor(e|ing)/i, /threshold/i, /count(ing)?/i, /filter/i,
  /regex/i, /keyword (match|matching)/i, /lookup/i, /persist/i, /audit log/i,
  /cron/i, /schedule/i, /timestamp/i, /hash (compute|verify)/i, /JSON (read|write|parse)/i
];
const LLM_INDICATORS = [
  /draft/i, /generate (a |an )?(natural|prose|message|email)/i, /synthesi(ze|s)/i, /interpret/i,
  /reason about/i, /explain/i, /summari(ze|se)/i, /extract (from|key|insight)/i, /tone/i, /voice/i,
  /paraphrase/i, /translate/i, /classify (sentiment|intent|nuance)/i
];

function classifyRequirement(req) {
  const text = (req.text || req.requirement_text || JSON.stringify(req)).toLowerCase();
  const detHits = DETERMINISTIC_INDICATORS.filter(r => r.test(text)).length;
  const llmHits = LLM_INDICATORS.filter(r => r.test(text)).length;

  let classification, rationale;
  if (detHits > 0 && llmHits === 0) {
    classification = 'deterministic';
    rationale = `Deterministic indicators matched (${detHits}); no LLM-only indicators. Implement as Node script.`;
  } else if (llmHits > 0 && detHits === 0) {
    classification = 'llm';
    rationale = `LLM-only indicators matched (${llmHits}); no purely deterministic indicators. Implement as llm-task with thinking: low.`;
  } else if (detHits > 0 && llmHits > 0) {
    classification = 'hybrid';
    rationale = `Both deterministic (${detHits}) and LLM (${llmHits}) indicators matched. Implement as Node script with llm-task fallback for ambiguous cases.`;
  } else {
    classification = 'unclear';
    rationale = `No strong indicators in either direction. Surface to user during Step 4 vet for explicit classification.`;
  }
  return { req_id: req.req_id || req.id, classification, rationale };
}

function projectLlmRatio(classifications) {
  const total = classifications.length;
  if (total === 0) return 0;
  const llmCount = classifications.filter(c => c.classification === 'llm').length;
  const hybridCount = classifications.filter(c => c.classification === 'hybrid').length;
  return (llmCount + hybridCount * 0.4) / total;
}

function readStdin() {
  try { return JSON.parse(fs.readFileSync(0, 'utf8')); } catch (e) { return {}; }
}

function main() {
  const stdinData = readStdin();
  const requirements = stdinData.requirements || stdinData.user_stories || [];

  const classifications = requirements.map(classifyRequirement);
  const llmRatio = projectLlmRatio(classifications);
  const exceedsWarnThreshold = llmRatio > LLM_RATIO_WARN_THRESHOLD;

  process.stdout.write(JSON.stringify({
    classifications,
    deterministic_count: classifications.filter(c => c.classification === 'deterministic').length,
    llm_count: classifications.filter(c => c.classification === 'llm').length,
    hybrid_count: classifications.filter(c => c.classification === 'hybrid').length,
    unclear_count: classifications.filter(c => c.classification === 'unclear').length,
    projected_llm_ratio: Math.round(llmRatio * 100) / 100,
    warn_threshold: LLM_RATIO_WARN_THRESHOLD,
    exceeds_warn_threshold: exceedsWarnThreshold,
    threshold_version: VERSION_TAG,
    warning_message: exceedsWarnThreshold ?
      `Projected LLM ratio (${Math.round(llmRatio * 100)}%) exceeds the v1 warn threshold (${LLM_RATIO_WARN_THRESHOLD * 100}%). User must explicitly acknowledge before build.` :
      null
  }));
}

if (require.main === module) main();

module.exports = { classifyRequirement, projectLlmRatio, LLM_RATIO_WARN_THRESHOLD };
