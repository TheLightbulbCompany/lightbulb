#!/usr/bin/env node
/**
 * pitch-meddic-gap-check.js
 * Requirement 36: Surface MEDDIC gaps when a deal advances pipeline stages.
 * Requirement 37: Never infer — only confirmed from prospect statements.
 *
 * Reads MEDDIC fields and checks confirmation status.
 * Deterministic. Zero LLM.
 */

const MEDDIC_FIELDS = [
  { key: 'metrics', label: 'Metrics', description: 'Quantified business impact' },
  { key: 'economic_buyer', label: 'Economic Buyer', description: 'Person with final purchase authority' },
  { key: 'decision_criteria', label: 'Decision Criteria', description: 'What the prospect is evaluating' },
  { key: 'decision_process', label: 'Decision Process', description: 'How the decision will be made' },
  { key: 'identify_pain', label: 'Identify Pain', description: 'Specific business problems expressed by prospect' },
  { key: 'champion', label: 'Champion', description: 'Internal advocate for the solution' }
];

// Which fields should be confirmed by which pipeline stage
const STAGE_REQUIREMENTS = {
  'discovery': ['identify_pain'],
  'qualification': ['identify_pain', 'economic_buyer'],
  'demo': ['identify_pain', 'economic_buyer', 'decision_criteria'],
  'proposal': ['identify_pain', 'economic_buyer', 'decision_criteria', 'decision_process', 'metrics'],
  'negotiation': ['identify_pain', 'economic_buyer', 'decision_criteria', 'decision_process', 'metrics', 'champion'],
  'closed_won': ['identify_pain', 'economic_buyer', 'decision_criteria', 'decision_process', 'metrics', 'champion'],
  'closed_lost': []
};

function checkMeddicGaps(deal) {
  const stage = (deal.stage || '').toLowerCase().replace(/\s+/g, '_');
  const meddicState = deal.meddic || {};
  const requiredFields = STAGE_REQUIREMENTS[stage] || [];

  const gaps = [];
  const confirmed = [];

  for (const field of MEDDIC_FIELDS) {
    const fieldData = meddicState[field.key];
    const isRequired = requiredFields.includes(field.key);

    if (!fieldData || !fieldData.confirmed) {
      if (isRequired) {
        gaps.push({
          field: field.key,
          label: field.label,
          status: fieldData ? 'inferred_not_confirmed' : 'missing',
          required_for_stage: stage,
          source: fieldData?.source || null,
          warning: fieldData
            ? `${field.label} has data but is not confirmed from a prospect statement. Source: ${fieldData.source || 'unknown'}.`
            : `${field.label} is missing. ${field.description}.`
        });
      }
    } else {
      confirmed.push({
        field: field.key,
        label: field.label,
        source: fieldData.source,
        confirmed_date: fieldData.confirmed_date
      });
    }
  }

  return {
    deal_name: deal.name || deal.domain,
    stage,
    has_gaps: gaps.length > 0,
    gaps,
    confirmed,
    gap_count: gaps.length,
    confirmed_count: confirmed.length,
    summary: gaps.length > 0
      ? `${deal.name || deal.domain} is at ${stage} stage with ${gaps.length} unconfirmed MEDDIC field(s): ${gaps.map(g => g.label).join(', ')}.`
      : `${deal.name || deal.domain} — all required MEDDIC fields confirmed for ${stage} stage.`
  };
}

async function main() {
  const input = await readStdin();
  if (!input) {
    process.stderr.write('No input provided');
    process.exit(1);
  }

  // Handle single deal or batch
  const deals = input.deals || [input.deal || input];

  const results = deals.map(deal => checkMeddicGaps(deal));
  const dealsWithGaps = results.filter(r => r.has_gaps);

  const output = {
    results,
    total_deals: results.length,
    deals_with_gaps: dealsWithGaps.length,
    deals_clear: results.length - dealsWithGaps.length,
    alerts: dealsWithGaps.map(d => d.summary),
    timestamp: new Date().toISOString()
  };

  process.stdout.write(JSON.stringify(output));
  process.exit(dealsWithGaps.length > 0 ? 1 : 0);
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

module.exports = { checkMeddicGaps };

if (require.main === module) {
  main().catch(err => {
    process.stderr.write(err.message);
    process.exit(1);
  });
}
