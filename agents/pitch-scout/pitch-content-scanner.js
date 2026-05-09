#!/usr/bin/env node
/**
 * pitch-content-scanner.js
 * Requirement 19: Validate signal-grounded opening
 * Requirement 25: Flag competitor, pricing, commitment, timeline references
 * Requirement 32: Catch non-response reference language
 *
 * Three checks in one script. Runs after draft generation, before send.
 * Deterministic regex/keyword matching. Zero LLM.
 */

// --- Requirement 25: Content that requires approval ---
const PRICING_KEYWORDS = [
  'price', 'pricing', 'cost', 'costs', 'discount', 'budget',
  'quote', 'proposal', 'investment', 'fee', 'fees', 'rate',
  '\\$\\d', '€\\d', '£\\d'
];

const COMMITMENT_KEYWORDS = [
  'guarantee', 'guaranteed', 'promise', 'commit', 'committed',
  'ensure', 'we will', 'we can deliver', 'we\'ll make sure',
  'you have my word', 'i assure', 'rest assured', 'without fail'
];

const TIMELINE_KEYWORDS = [
  'by \\w+ \\d+', 'within \\d+', 'deadline', 'timeline',
  'eta', 'delivery date', 'go-live', 'launch date',
  'ready by', 'completed by', 'no later than'
];

// --- Requirement 32: Non-response reference language ---
const NON_RESPONSE_PATTERNS = [
  'following up', 'follow up', 'followed up',
  'haven\'t heard', 'have not heard', 'didn\'t hear',
  'reaching out again', 'reach out again',
  'previous email', 'previous message', 'last email', 'last message',
  'checking in', 'just checking', 'touching base',
  'circling back', 'wanted to circle',
  'sent you a couple', 'sent you a few',
  'no response', 'no reply',
  'i know you\'re busy', 'i understand you\'re busy'
];

function buildRegex(patterns) {
  return new RegExp(patterns.join('|'), 'gi');
}

function scanContent(draft, config) {
  const flags = [];
  const body = draft.body || draft.text || '';
  const subject = draft.subject || '';
  const fullText = `${subject} ${body}`.toLowerCase();

  // --- Requirement 25: Competitor names ---
  const competitors = config.competitor_names || [];
  const competitorMatches = competitors.filter(c =>
    fullText.includes(c.toLowerCase())
  );
  if (competitorMatches.length > 0) {
    flags.push({
      type: 'competitor_reference',
      severity: 'approval_required',
      matches: competitorMatches,
      reason: 'Message references competitor(s). Requires rep approval per PRD — creates commercial risk.'
    });
  }

  // --- Requirement 25: Pricing ---
  const pricingRegex = buildRegex(PRICING_KEYWORDS);
  const pricingMatches = fullText.match(pricingRegex);
  if (pricingMatches) {
    flags.push({
      type: 'pricing_reference',
      severity: 'approval_required',
      matches: [...new Set(pricingMatches)],
      reason: 'Message references pricing or costs. Requires rep approval — creates commercial obligations.'
    });
  }

  // --- Requirement 25: Commitments ---
  const commitmentRegex = buildRegex(COMMITMENT_KEYWORDS);
  const commitmentMatches = fullText.match(commitmentRegex);
  if (commitmentMatches) {
    flags.push({
      type: 'commitment_language',
      severity: 'approval_required',
      matches: [...new Set(commitmentMatches)],
      reason: 'Message contains commitment language. Requires rep approval — creates obligations under rep\'s name.'
    });
  }

  // --- Requirement 25: Timeline guarantees ---
  const timelineRegex = buildRegex(TIMELINE_KEYWORDS);
  const timelineMatches = fullText.match(timelineRegex);
  if (timelineMatches) {
    flags.push({
      type: 'timeline_guarantee',
      severity: 'approval_required',
      matches: [...new Set(timelineMatches)],
      reason: 'Message contains timeline language. Requires rep approval — creates delivery expectations.'
    });
  }

  // --- Requirement 32: Non-response references ---
  const nonResponseRegex = buildRegex(NON_RESPONSE_PATTERNS);
  const nonResponseMatches = fullText.match(nonResponseRegex);
  if (nonResponseMatches) {
    flags.push({
      type: 'non_response_reference',
      severity: 'reject_and_regenerate',
      matches: [...new Set(nonResponseMatches)],
      reason: 'Message references prospect\'s non-response. This is the most common trigger for unsubscribes from AI sequences. Reject and regenerate with new angle.'
    });
  }

  // --- Requirement 19: Signal grounding validation ---
  if (config.signal_terms && config.signal_terms.length > 0) {
    const signalFound = config.signal_terms.some(term =>
      fullText.includes(term.toLowerCase())
    );
    if (!signalFound) {
      flags.push({
        type: 'missing_signal_grounding',
        severity: 'reject_and_regenerate',
        reason: 'Draft does not reference the specific triggering signal. Generic openers are the authenticity gap the PRD is built to prevent. Regenerate with explicit signal reference.'
      });
    }
  }

  // --- Escape hatch: flag drafts that passed keyword checks but may have subtle issues ---
  // The keyword lists catch explicit patterns. But subtle competitor references ("unlike tools
  // that require manual setup"), implicit commitments ("you'll see results within weeks"), and
  // tone mismatches that don't contain flagged words should route to the agent loop.
  // When a draft is long (>200 words) or the prospect is high-value, recommend agent loop review
  // even if no keywords matched, because the cost of a subtle mistake in a high-stakes message
  // exceeds the cost of one LLM review call.
  const isHighStakes = config.prospect_tier === 'enterprise' || config.prospect_tier === 'strategic' ||
                       config.is_first_touch || config.prospect_title_seniority === 'c_suite';
  const isLongDraft = body.split(/\s+/).length > 200;

  if (flags.length === 0 && (isHighStakes || isLongDraft)) {
    flags.push({
      type: 'recommend_agent_review',
      severity: 'advisory',
      reason: isHighStakes
        ? 'High-stakes prospect — recommend agent loop review for tone, subtlety, and implicit commitments the keyword scanner may miss.'
        : 'Long draft — recommend agent loop review for content coherence and implicit patterns.',
      needs_agent_loop: true
    });
  }

  return flags;
}

async function main() {
  const input = await readStdin();
  if (!input) {
    process.stderr.write('No input provided');
    process.exit(1);
  }

  const draft = input.draft || {};
  const config = input.config || {};

  const flags = scanContent(draft, config);

  const approvalRequired = flags.some(f => f.severity === 'approval_required');
  const rejectAndRegenerate = flags.some(f => f.severity === 'reject_and_regenerate');

  const result = {
    pass: flags.length === 0 || flags.every(f => f.severity === 'advisory'),
    flags,
    action: rejectAndRegenerate ? 'regenerate' :
            approvalRequired ? 'route_to_approval' :
            flags.some(f => f.needs_agent_loop) ? 'recommend_agent_review' :
            'proceed',
    needs_agent_loop: flags.some(f => f.needs_agent_loop),
    draft_domain: draft.prospect_domain || 'unknown',
    timestamp: new Date().toISOString()
  };

  process.stdout.write(JSON.stringify(result));
  process.exit(flags.length === 0 ? 0 : 1);
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

module.exports = { scanContent };

if (require.main === module) {
  main().catch(err => {
    process.stderr.write(err.message);
    process.exit(1);
  });
}
