#!/usr/bin/env node
// atlas-tier-cost-estimator.js
//
// Applies tier rate card to LLM call counts. Per L4 markup is line-itemed not buried;
// per L8 what-if comparison visually separated in output card; per L3 warning shows
// dollar delta not just multiplier.
//
// Output is the canonical Phase 6 cost-impact card per TOOLS.md § Cost Output Formatting.

const fs = require('fs');

// Token estimates per LLM call (PROJECTED, NOT TESTED — per H5)
// 5K-token-average baseline is the BLUNT INSTRUMENT documented in TOOLS.md.
// Real cost calibration requires 5+ measured builds.
const TOKEN_ESTIMATES = Object.freeze({
  compile_research_brief: { input: 5000, output: 1000, frequency_per_build: 1 },
  archetype_synthesis:    { input: 8000, output: 2000, frequency_per_build: 1 },
  overlap_disambig:       { input: 2000, output: 400,  frequency_per_build: 0.4 } // conditional
});

// Per-tier rate card (PROJECTED, approximate as of 2026-04 — verify before relying on dollar figures)
const RATE_CARDS = Object.freeze({
  byo_openai: {
    provider: 'OpenAI',
    model: 'gpt-5-mini',
    input_per_1m: 0.40,
    output_per_1m: 1.60,
    markup: 1.0
  },
  byo_claude: {
    provider: 'Anthropic',
    model: 'claude-haiku-4-5',
    input_per_1m: 0.80,
    output_per_1m: 4.00,
    markup: 1.0
  },
  hosted_claude: {
    provider: 'Anthropic (via isol8 proxy)',
    model: 'claude-haiku-4-5',
    input_per_1m: 0.80,
    output_per_1m: 4.00,
    markup: 1.5
  }
});

const ALL_LLM_BASELINE_TOKENS_PER_DECISION = 5000; // BLUNT INSTRUMENT per TOOLS.md

function readStdin() {
  try { return JSON.parse(fs.readFileSync(0, 'utf8')); } catch (e) { return {}; }
}

function calculateCost(tokensIn, tokensOut, rateCard) {
  const baseInput = (tokensIn / 1_000_000) * rateCard.input_per_1m;
  const baseOutput = (tokensOut / 1_000_000) * rateCard.output_per_1m;
  const baseTotal = baseInput + baseOutput;
  const markedUp = baseTotal * rateCard.markup;
  return {
    base_input_cost: Math.round(baseInput * 10000) / 10000,
    base_output_cost: Math.round(baseOutput * 10000) / 10000,
    base_total: Math.round(baseTotal * 10000) / 10000,
    markup_amount: Math.round((markedUp - baseTotal) * 10000) / 10000,
    final_total: Math.round(markedUp * 10000) / 10000
  };
}

function projectAtlasCostPerBuild(rateCard) {
  let totalIn = 0, totalOut = 0;
  for (const [_, est] of Object.entries(TOKEN_ESTIMATES)) {
    totalIn += est.input * est.frequency_per_build;
    totalOut += est.output * est.frequency_per_build;
  }
  return {
    total_tokens_input: totalIn,
    total_tokens_output: totalOut,
    cost: calculateCost(totalIn, totalOut, rateCard)
  };
}

function projectAllLlmBaseline(replacementCount, rateCard) {
  const totalDecisions = replacementCount + 2.4; // replaced + Atlas's actual ~2.4 calls
  const tokenInPerDecision = ALL_LLM_BASELINE_TOKENS_PER_DECISION * 0.7;
  const tokenOutPerDecision = ALL_LLM_BASELINE_TOKENS_PER_DECISION * 0.3;
  const totalIn = totalDecisions * tokenInPerDecision;
  const totalOut = totalDecisions * tokenOutPerDecision;
  return {
    total_decisions: totalDecisions,
    total_tokens_input: totalIn,
    total_tokens_output: totalOut,
    cost: calculateCost(totalIn, totalOut, rateCard)
  };
}

function formatDeltaText(atlas, baseline) {
  const savedPerBuild = baseline.cost.final_total - atlas.cost.final_total;
  return `~$${atlas.cost.final_total.toFixed(3)}/build vs ~$${baseline.cost.final_total.toFixed(2)}/build all-LLM baseline → ~$${savedPerBuild.toFixed(2)} saved per build (PROJECTED, NOT TESTED — 5K-token-average baseline could be 2x high or 2x low; markup line-itemed: $${atlas.cost.base_total.toFixed(3)} provider + $${atlas.cost.markup_amount.toFixed(3)} markup = $${atlas.cost.final_total.toFixed(3)})`;
}

function main() {
  const stdinData = readStdin();
  const tierInfo = stdinData.tier || {};
  const countInfo = stdinData.count || {};
  const tier = tierInfo.tier || 'byo_claude';
  const replacementCount = countInfo.replacement_count || 100;

  const rateCard = RATE_CARDS[tier];
  if (!rateCard) {
    process.stdout.write(JSON.stringify({ error: `unknown tier: ${tier}` }));
    process.exit(1);
  }

  const atlasProjection = projectAtlasCostPerBuild(rateCard);
  const baselineProjection = projectAllLlmBaseline(replacementCount, rateCard);

  // L7: deterministic-replacement count first (tier-agnostic)
  // L8: per-tier $ delta at user's active tier only
  // L4: markup line-itemed
  // Per H5: labeled "projected" never "tested"
  const card = {
    label: 'projected_not_tested',
    architectural_signal: {
      replacement_count: replacementCount,
      replacement_ratio_percent: countInfo.replacement_ratio || null,
      note: 'Tier-agnostic. Comparable within archetype, not across archetypes (per TOOLS.md footer).'
    },
    active_tier: {
      tier: tier,
      provider: rateCard.provider,
      model: rateCard.model,
      atlas_runtime_per_build: atlasProjection.cost,
      all_llm_baseline_per_build: baselineProjection.cost,
      delta_text: formatDeltaText(atlasProjection, baselineProjection)
    },
    transparency: {
      assumption_set: [
        'Token counts per call estimated from prior-agent pipeline patterns, not measured on Atlas',
        'All-LLM baseline assumes 5K tokens average per replaced decision — could be 2x high or 2x low',
        'overlap-disambig firing rate (0.4/build) is a guess; will calibrate from real builds',
        'Per-tier rate cards approximate as of 2026-04',
        'Per-build matrix size varies by user request'
      ],
      what_if_comparison_separator: '─── What-if: not your active tier (separately formatted per L8) ───'
    }
  };

  process.stdout.write(JSON.stringify(card));
}

if (require.main === module) main();

module.exports = { TOKEN_ESTIMATES, RATE_CARDS, calculateCost };
