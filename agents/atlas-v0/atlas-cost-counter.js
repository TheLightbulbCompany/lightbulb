#!/usr/bin/env node
// atlas-cost-counter.js
//
// Counts deterministic vs LLM nodes per pipeline; emits architectural-replacement count
// tier-agnostic per L7. Outputs labeled "projected" never "tested" per H5.
//
// Modes: --build-id=<id> (default; counts for a specific build), --rollup-day (cron)

const fs = require('fs');
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
  // Count Atlas's runtime nodes per build with a matrix size of N
  // (architectural baseline; tracked in matrix snapshot)
  const matrix = fastIoRead(`atlas-builds/${buildId}/requirement-matrix`);
  const requirementCount = matrix ? ((matrix.user_stories || matrix.classifications || []).length) : 60;

  // Atlas's actual LLM calls per build (typical, per Phase 6 § 6.1):
  //   - compile-research-brief: 1 (Phase 2, thinking: low)
  //   - archetype-synthesis:    1 (Phase 3, thinking: low)
  //   - overlap-disambig:       0.4 average (Phase 1, conditional, thinking: off)
  const llmCalls = 1 + 1 + 0.4;

  // Atlas's deterministic Node-script invocations per build:
  //   Phase 1: identity-lock, tier-detector, stack-overlap (3)
  //   Phase 2: cookbook-reader, failure-case-router, summarize compress, persist-brief (4)
  //   Phase 3: skeleton-generator, det-llm-classifier, skill-recommender, snapshot-matrix, snapshot-sha256, present-audit (6)
  //   Phase 4: requirement-walker iterations (1 init + N walker + N record + N taskr-update + 1 finalize = 3N+2)
  //   Phase 5: verify-matrix-hash, recap-diff, persist-recap (3)
  //   Phase 6: cost-count, load-tier, tier-cost-estimate, persist-cost, present-summary (5)
  //   Phase 7: clawhavoc-scan, sensitive-sniff, file-write-gate, file-generate, verify-write (5)
  //   Phase 8 (optional): clawhavoc, sniff, listing-build, bundle-rehash, continuity-check, submit-gate, submit (7)
  const phase4Walker = 3 * requirementCount + 2;
  const detNodes = 3 + 4 + 6 + phase4Walker + 3 + 5 + 5; // Phase 8 optional, not always counted
  // All-LLM hypothetical baseline: every decision becomes an LLM call
  // Decisions per build = phases × decision points + matrix walker (N)
  const allLlmBaseline = 3 + 3 + 3 + requirementCount + 1 + 1 + 33 + 4; // ~108 with 60-row matrix

  const replacementCount = allLlmBaseline - llmCalls;
  const replacementRatio = (replacementCount / allLlmBaseline);

  return {
    build_id: buildId,
    requirement_matrix_size: requirementCount,
    actual_llm_calls: llmCalls,
    actual_deterministic_nodes: detNodes,
    all_llm_hypothetical_baseline: allLlmBaseline,
    replacement_count: Math.round(replacementCount),
    replacement_ratio: Math.round(replacementRatio * 1000) / 10, // percent with 1 decimal
    note: `Architectural ratio (~${Math.round(replacementRatio * 100)}%) is tier-agnostic per L7. PROJECTED, NOT TESTED per H5. Archetype-specific — see TOOLS.md § Cost Output Formatting for cross-archetype caveat.`,
    label: 'projected',
    archetype_specific_note: 'Comparable within archetype, not across archetypes. Atlas is meta-agent-builder archetype.'
  };
}

function rollupDay() {
  // Daily 23:00 cron: rollup of the day's audit log into a digest
  // Stub: real implementation queries audit/{{date}}/* and produces digest
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
