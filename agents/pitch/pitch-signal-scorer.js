#!/usr/bin/env node
/**
 * pitch-signal-scorer.js
 * Requirement 10: Composite signal scoring.
 *
 * Scores three dimensions deterministically (ICP fit, recency, relationship history).
 * Signal strength dimension is left for llm-task (requires language interpretation).
 * Deterministic for 75% of scoring. Zero LLM.
 */

const WEIGHTS = {
  icp_fit: 0.35,
  signal_strength: 0.30,  // populated by llm-task externally
  recency: 0.20,
  relationship: 0.15
};

function scoreIcpFit(prospect, icpCriteria) {
  let score = 0;
  let maxScore = 0;

  // Company size
  if (icpCriteria.company_size_min && icpCriteria.company_size_max) {
    maxScore += 25;
    if (prospect.company_size >= icpCriteria.company_size_min &&
        prospect.company_size <= icpCriteria.company_size_max) {
      score += 25;
    } else if (prospect.company_size >= icpCriteria.company_size_min * 0.5 &&
               prospect.company_size <= icpCriteria.company_size_max * 1.5) {
      score += 10; // partial match
    }
  }

  // Funding stage
  if (icpCriteria.funding_stages && icpCriteria.funding_stages.length > 0) {
    maxScore += 20;
    if (icpCriteria.funding_stages.includes(prospect.funding_stage)) {
      score += 20;
    }
  }

  // Geography
  if (icpCriteria.geographies && icpCriteria.geographies.length > 0) {
    maxScore += 15;
    if (icpCriteria.geographies.includes(prospect.geography)) {
      score += 15;
    }
  }

  // Industry
  if (icpCriteria.industries && icpCriteria.industries.length > 0) {
    maxScore += 20;
    if (icpCriteria.industries.includes(prospect.industry)) {
      score += 20;
    }
  }

  // Tech stack overlap
  if (icpCriteria.tech_stack_signals && icpCriteria.tech_stack_signals.length > 0) {
    maxScore += 20;
    const prospectStack = prospect.tech_stack || [];
    const overlap = icpCriteria.tech_stack_signals.filter(t =>
      prospectStack.some(ps => ps.toLowerCase().includes(t.toLowerCase()))
    );
    score += Math.round((overlap.length / icpCriteria.tech_stack_signals.length) * 20);
  }

  return maxScore > 0 ? Math.round((score / maxScore) * 100) : 0;
}

function scoreRecency(signalTimestamp) {
  const hoursAgo = (Date.now() - new Date(signalTimestamp).getTime()) / (1000 * 60 * 60);

  // Exponential decay: full score within 4 hours, drops to ~20% at 72 hours
  if (hoursAgo <= 4) return 100;
  if (hoursAgo <= 12) return 85;
  if (hoursAgo <= 24) return 70;
  if (hoursAgo <= 48) return 50;
  if (hoursAgo <= 72) return 30;
  if (hoursAgo <= 168) return 15; // 1 week
  return 5;
}

function scoreRelationship(crmHistory) {
  if (!crmHistory || crmHistory.length === 0) return 50; // neutral — new prospect

  let score = 50;

  // Prior positive engagement boosts
  const positiveOutcomes = crmHistory.filter(h =>
    ['replied', 'meeting_booked', 'interested', 'engaged'].includes(h.outcome)
  );
  score += positiveOutcomes.length * 10;

  // Closed-lost without re-engagement penalizes
  const closedLost = crmHistory.filter(h => h.outcome === 'closed_lost');
  const reEngaged = crmHistory.filter(h =>
    h.outcome === 'engaged' &&
    closedLost.some(cl => new Date(h.date) > new Date(cl.date))
  );
  if (closedLost.length > 0 && reEngaged.length === 0) {
    score -= 20;
  }

  // Recent contact (within 90 days) — slight boost
  const recentContact = crmHistory.some(h => {
    const daysAgo = (Date.now() - new Date(h.date).getTime()) / (1000 * 60 * 60 * 24);
    return daysAgo <= 90;
  });
  if (recentContact) score += 5;

  return Math.max(0, Math.min(100, score));
}

function computeComposite(scores) {
  return Math.round(
    scores.icp_fit * WEIGHTS.icp_fit +
    scores.signal_strength * WEIGHTS.signal_strength +
    scores.recency * WEIGHTS.recency +
    scores.relationship * WEIGHTS.relationship
  );
}

async function main() {
  const input = await readStdin();
  if (!input) {
    process.stderr.write('No input provided');
    process.exit(1);
  }

  const signals = input.signals || [];
  const icpCriteria = input.icp_criteria || {};
  const threshold = input.threshold || 60;

  const scored = signals.map(signal => {
    const scores = {
      icp_fit: scoreIcpFit(signal.prospect || {}, icpCriteria),
      recency: scoreRecency(signal.timestamp),
      relationship: scoreRelationship(signal.crm_history || []),
      signal_strength: signal.llm_strength_score || 50 // default if not yet scored by llm-task
    };

    const composite = computeComposite(scores);

    // Escape hatch: flag signals that need agent loop judgment
    const needsAgentLoop =
      // Novel signal type the scorer doesn't have patterns for
      (!signal.signal_type || signal.signal_type === 'unknown') ||
      // ICP data too sparse to score meaningfully
      (scores.icp_fit === 50 && !signal.prospect?.company_size && !signal.prospect?.industry) ||
      // Borderline — within 5 points of threshold, agent should weigh in
      (Math.abs(composite - threshold) <= 5) ||
      // Compound signal (multiple signal types on same prospect)
      (signal.compound_signals && signal.compound_signals.length > 1);

    return {
      ...signal,
      scores,
      composite_score: composite,
      qualified: composite >= threshold,
      needs_agent_loop: needsAgentLoop,
      agent_loop_reason: needsAgentLoop
        ? ((!signal.signal_type || signal.signal_type === 'unknown') ? 'novel_signal_type' :
           scores.icp_fit === 50 ? 'sparse_icp_data' :
           Math.abs(composite - threshold) <= 5 ? 'borderline_score' :
           'compound_signal')
        : null
    };
  });

  const qualified = scored.filter(s => s.qualified);
  const filtered = scored.filter(s => !s.qualified);

  const result = {
    qualified,
    filtered,
    total: signals.length,
    qualified_count: qualified.length,
    filtered_count: filtered.length,
    threshold,
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

module.exports = { scoreIcpFit, scoreRecency, scoreRelationship, computeComposite };

if (require.main === module) {
  main().catch(err => {
    process.stderr.write(err.message);
    process.exit(1);
  });
}
