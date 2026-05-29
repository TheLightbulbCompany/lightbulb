#!/usr/bin/env node
// atlas-cost-counter.js
//
// Counts deterministic vs LLM nodes per pipeline; emits architectural-replacement count.
// Outputs labeled "projected" never "tested" per H5.

const { execSync } = require('child_process');

function parseArgs(argv) {
  const args = { buildId: null, rollupDay: false };
  for (const a of argv.slice(2)) {
    if (a.startsWith('--build-id=')) args.buildId = a.split('=')[1];
    else if (a === '--rollup-day') args.rollupDay = true;
  }
  return args;
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

function countNodesForBuild(buildId) {
  const matrix = fastIoRead(`atlas-builds/${buildId}/requirement-matrix`);
  const requirementCount = matrix ? ((matrix.user_stories || matrix.classifications || []).length) : 60;

  // Atlas's actual LLM calls per build (typical):
  //   - compile-research-brief: 1 (Step 1, thinking: low)
  //   - archetype-synthesis:    1 (Step 2, thinking: low)
  //   - overlap-disambig:       0.4 average (Step 1, conditional, thinking: off)
  const llmCalls = 1 + 1 + 0.4;

  // Atlas's deterministic Node-script invocations per build:
  const phase4Walker = 3 * requirementCount + 2;
  const detNodes = 3 + 4 + 6 + phase4Walker + 3 + 5 + 5;
  const allLlmBaseline = 3 + 3 + 3 + requirementCount + 1 + 1 + 33 + 4;

  const replacementCount = allLlmBaseline - llmCalls;
  const replacementRatio = (replacementCount / allLlmBaseline);

  return {
    build_id: buildId,
    requirement_matrix_size: requirementCount,
    actual_llm_calls: llmCalls,
    actual_deterministic_nodes: detNodes,
    all_llm_hypothetical_baseline: allLlmBaseline,
    replacement_count: Math.round(replacementCount),
    replacement_ratio: Math.round(replacementRatio * 1000) / 10,
    note: `Architectural ratio (~${Math.round(replacementRatio * 100)}%). PROJECTED, NOT TESTED per H5. Archetype-specific.`,
    label: 'projected',
    archetype_specific_note: 'Comparable within archetype, not across archetypes.'
  };
}

function rollupDay() {
  return {
    action: 'audit_rollup',
    date: new Date().toISOString().slice(0, 10),
    note: 'Real implementation queries audit/{{date}}/* and emits per-action counts.'
  };
}

function main() {
  const args = parseArgs(process.argv);
  if (args.rollupDay) {
    process.stdout.write(JSON.stringify(rollupDay()));
    return;
  }
  if (!args.buildId) { console.error('--build-id required'); process.exit(2); }
  process.stdout.write(JSON.stringify(countNodesForBuild(args.buildId)));
}

if (require.main === module) main();

module.exports = { countNodesForBuild };
