#!/usr/bin/env node
/**
 * pitch-icp-router.js
 * Requirement 16: Archive sub-threshold prospects with visible scoring breakdown.
 *
 * Deterministic routing based on ICP score. Zero LLM.
 * Also handles ICP scoring itself (replaces llm-task call for cost savings).
 */

const { scoreIcpFit } = require('./pitch-signal-scorer.js');

const DEFAULT_THRESHOLD = 60;

async function main() {
  const input = await readStdin();
  if (!input) {
    process.stderr.write('No input provided');
    process.exit(1);
  }

  const prospect = input.prospect || {};
  const icpCriteria = input.icp_criteria || {};
  const enrichmentData = input.enrichment_data || {};
  const threshold = input.threshold || DEFAULT_THRESHOLD;

  // Score ICP fit deterministically
  const score = scoreIcpFit(enrichmentData, icpCriteria);

  // Build breakdown showing which criteria passed/failed
  const breakdown = {};
  if (icpCriteria.company_size_min && icpCriteria.company_size_max) {
    const inRange = enrichmentData.company_size >= icpCriteria.company_size_min &&
                    enrichmentData.company_size <= icpCriteria.company_size_max;
    breakdown.company_size = {
      criteria: `${icpCriteria.company_size_min}-${icpCriteria.company_size_max}`,
      actual: enrichmentData.company_size || 'unknown',
      pass: inRange
    };
  }
  if (icpCriteria.funding_stages) {
    breakdown.funding_stage = {
      criteria: icpCriteria.funding_stages,
      actual: enrichmentData.funding_stage || 'unknown',
      pass: icpCriteria.funding_stages.includes(enrichmentData.funding_stage)
    };
  }
  if (icpCriteria.geographies) {
    breakdown.geography = {
      criteria: icpCriteria.geographies,
      actual: enrichmentData.geography || 'unknown',
      pass: icpCriteria.geographies.includes(enrichmentData.geography)
    };
  }
  if (icpCriteria.industries) {
    breakdown.industry = {
      criteria: icpCriteria.industries,
      actual: enrichmentData.industry || 'unknown',
      pass: icpCriteria.industries.includes(enrichmentData.industry)
    };
  }
  if (icpCriteria.tech_stack_signals) {
    const prospectStack = enrichmentData.tech_stack || [];
    const overlap = icpCriteria.tech_stack_signals.filter(t =>
      prospectStack.some(ps => ps.toLowerCase().includes(t.toLowerCase()))
    );
    breakdown.tech_stack = {
      criteria: icpCriteria.tech_stack_signals,
      matched: overlap,
      pass: overlap.length > 0
    };
  }

  const qualified = score >= threshold;
  const destination = qualified
    ? `briefs/active/${prospect.domain}`
    : `briefs/archived/${prospect.domain}`;

  const result = {
    domain: prospect.domain,
    score,
    threshold,
    qualified,
    destination,
    breakdown,
    timestamp: new Date().toISOString()
  };

  process.stdout.write(JSON.stringify(result));
  process.exit(0);
}

function readStdin() {
  return new Promise((resolve) => {
    let data = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', chunk => data += chunk);
    process.stdin.on('end', () => {
      try { resolve(JSON.parse(data)); }
      catch { resolve(null); }
    });
    if (process.stdin.isTTY) resolve(null);
  });
}

if (require.main === module) {
  main().catch(err => {
    process.stderr.write(err.message);
    process.exit(1);
  });
}
