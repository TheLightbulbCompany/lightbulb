#!/usr/bin/env node
/**
 * scout-icp-scorer.js
 * Requirements 35, 36, 38: Score 0-100, route by tier, penalize shared databases.
 *
 * Deterministic weighted math. Zero LLM.
 */

const WEIGHTS = {
  firmographic: 0.25,
  technographic: 0.15,
  role_fit: 0.20,
  intent_strength: 0.25,
  recency: 0.15
};

const DEFAULT_EXCLUSIVITY = {
  visitor_identification: 1.0,
  reddit_pain: 0.9,
  review_displacement: 0.85,
  job_posting_intent: 0.8,
  funding_signal: 0.7,
  news_trigger: 0.7,
  bombora_intent: 0.6,
  '6sense_intent': 0.6,
  apollo_funding: 0.65,
  apollo_database: 0.4,
  zoominfo_database: 0.4,
  perplexity_news: 0.55,
  default: 0.5
};

const SCORE_TIERS = {
  priority: { min: 75, queue: 'lead-queue/priority' },
  standard: { min: 50, queue: 'lead-queue/standard' },
  archive: { min: 0, queue: 'lead-archive' }
};

function scoreFirmographic(lead, icp) {
  let score = 0;
  let maxScore = 0;

  if (icp.company_size) {
    maxScore += 30;
    if (lead.company_size >= (icp.company_size.min || 0) &&
        lead.company_size <= (icp.company_size.max || Infinity)) {
      score += 30;
    } else if (lead.company_size >= (icp.company_size.min || 0) * 0.5 &&
               lead.company_size <= (icp.company_size.max || Infinity) * 1.5) {
      score += 12;
    }
  }

  if (icp.funding_stages && icp.funding_stages.length > 0) {
    maxScore += 25;
    if (icp.funding_stages.includes(lead.funding_stage)) score += 25;
  }

  if (icp.geographies && icp.geographies.length > 0) {
    maxScore += 20;
    if (icp.geographies.includes(lead.geography)) score += 20;
  }

  if (icp.industries && icp.industries.length > 0) {
    maxScore += 25;
    if (icp.industries.includes(lead.industry)) score += 25;
  }

  return maxScore > 0 ? Math.round((score / maxScore) * 100) : 50;
}

function scoreTechnographic(lead, icp) {
  if (!icp.tech_stack_signals || icp.tech_stack_signals.length === 0) return 50;
  const leadStack = lead.tech_stack || [];
  if (leadStack.length === 0) return 30;

  const overlap = icp.tech_stack_signals.filter(t =>
    leadStack.some(ls => ls.toLowerCase().includes(t.toLowerCase()))
  );
  return Math.round((overlap.length / icp.tech_stack_signals.length) * 100);
}

function scoreRoleFit(lead, icp) {
  if (!icp.target_roles || icp.target_roles.length === 0) return 50;
  const leadTitle = (lead.title || '').toLowerCase();

  // Exact match
  if (icp.target_roles.some(r => leadTitle.includes(r.toLowerCase()))) return 100;

  // Seniority match (VP, Director, Head of, C-level)
  const seniorityKeywords = ['vp', 'vice president', 'director', 'head of', 'chief', 'cto', 'cfo', 'ceo', 'coo', 'cro'];
  const hasSeniority = seniorityKeywords.some(k => leadTitle.includes(k));
  if (hasSeniority) return 60;

  // Manager level
  if (leadTitle.includes('manager') || leadTitle.includes('lead')) return 40;

  return 20;
}

function scoreIntentStrength(lead) {
  // Intent score comes from the signal data — Bombora surge score, 6sense buying stage, etc.
  if (lead.bombora_surge_score) {
    return Math.min(100, lead.bombora_surge_score);
  }
  if (lead.sixsense_buying_stage) {
    const stageScores = {
      'target': 20,
      'awareness': 40,
      'consideration': 60,
      'decision': 85,
      'purchase': 100
    };
    return stageScores[lead.sixsense_buying_stage.toLowerCase()] || 50;
  }
  if (lead.signal_strength) {
    return lead.signal_strength;
  }
  return 50;
}

function scoreRecency(signalTimestamp) {
  if (!signalTimestamp) return 30;
  const hoursAgo = (Date.now() - new Date(signalTimestamp).getTime()) / (1000 * 60 * 60);

  if (hoursAgo <= 4) return 100;
  if (hoursAgo <= 12) return 90;
  if (hoursAgo <= 24) return 80;
  if (hoursAgo <= 48) return 65;
  if (hoursAgo <= 72) return 50;
  if (hoursAgo <= 168) return 30;
  if (hoursAgo <= 336) return 15;
  return 5;
}

function getExclusivityFactor(signalSource, customWeights) {
  const weights = { ...DEFAULT_EXCLUSIVITY, ...customWeights };
  return weights[signalSource] || weights.default;
}

function getTier(score) {
  if (score >= SCORE_TIERS.priority.min) return { tier: 'priority', queue: SCORE_TIERS.priority.queue };
  if (score >= SCORE_TIERS.standard.min) return { tier: 'standard', queue: SCORE_TIERS.standard.queue };
  return { tier: 'archive', queue: SCORE_TIERS.archive.queue };
}

function scoreLead(lead, icp, config) {
  const dimensions = {
    firmographic: scoreFirmographic(lead, icp),
    technographic: scoreTechnographic(lead, icp),
    role_fit: scoreRoleFit(lead, icp),
    intent_strength: scoreIntentStrength(lead),
    recency: scoreRecency(lead.signal_timestamp)
  };

  // Support custom dimensions from user config (escape hatch for unique ICPs)
  // Custom dimensions are scored by the agent loop and passed in as pre-computed values
  const customDimensions = config.custom_dimensions || {};
  const customWeightTotal = Object.values(customDimensions).reduce((sum, d) => sum + (d.weight || 0), 0);

  // Rebalance weights if custom dimensions exist
  const effectiveWeights = { ...WEIGHTS };
  if (customWeightTotal > 0) {
    const scaleFactor = (1 - customWeightTotal) / 1; // shrink default weights to make room
    for (const key of Object.keys(effectiveWeights)) {
      effectiveWeights[key] *= scaleFactor;
    }
  }

  // Weighted composite with both default and custom dimensions
  let rawScore = Math.round(
    dimensions.firmographic * effectiveWeights.firmographic +
    dimensions.technographic * effectiveWeights.technographic +
    dimensions.role_fit * effectiveWeights.role_fit +
    dimensions.intent_strength * effectiveWeights.intent_strength +
    dimensions.recency * effectiveWeights.recency +
    Object.values(customDimensions).reduce((sum, d) => sum + ((d.score || 50) * (d.weight || 0)), 0)
  );

  // Add custom dimensions to the output for traceability
  for (const [name, d] of Object.entries(customDimensions)) {
    dimensions[name] = d.score || 50;
  }

  // Apply source exclusivity factor (Req 38)
  const exclusivity = getExclusivityFactor(lead.signal_source, config.exclusivity_weights);
  const exclusivityAdjustment = Math.round((exclusivity - 0.5) * 20); // -10 to +10 range
  const finalScore = Math.max(0, Math.min(100, rawScore + exclusivityAdjustment));

  // Apply visitor identification priority boost (Req 24)
  const visitorBoost = lead.signal_source === 'visitor_identification' ? (config.visitor_boost || 20) : 0;
  const boostedScore = Math.min(100, finalScore + visitorBoost);

  const tier = getTier(boostedScore);

  return {
    domain: lead.domain,
    score: boostedScore,
    raw_score: rawScore,
    dimensions,
    exclusivity_factor: exclusivity,
    exclusivity_adjustment: exclusivityAdjustment,
    visitor_boost: visitorBoost,
    tier: tier.tier,
    queue: tier.queue,
    urgency: lead.signal_source === 'visitor_identification' || boostedScore >= 90,
    signal_source: lead.signal_source
  };
}

async function main() {
  const input = await readStdin();
  if (!input) {
    process.stderr.write('No input provided');
    process.exit(1);
  }

  const leads = input.leads || [input.lead || input];
  const icp = input.icp || {};
  const config = input.config || {};

  const scored = leads.map(lead => scoreLead(lead, icp, config));

  const result = {
    scored,
    summary: {
      total: scored.length,
      priority: scored.filter(s => s.tier === 'priority').length,
      standard: scored.filter(s => s.tier === 'standard').length,
      archive: scored.filter(s => s.tier === 'archive').length,
      urgent: scored.filter(s => s.urgency).length
    },
    timestamp: new Date().toISOString()
  };

  process.stdout.write(JSON.stringify(result));
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

module.exports = { scoreLead, getTier, getExclusivityFactor };

if (require.main === module) {
  main().catch(err => {
    process.stderr.write(err.message);
    process.exit(1);
  });
}
