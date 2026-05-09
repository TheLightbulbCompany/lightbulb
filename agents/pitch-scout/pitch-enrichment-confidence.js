#!/usr/bin/env node
/**
 * pitch-enrichment-confidence.js
 * Requirement 15: Flag and quarantine contacts where enrichment confidence falls below threshold.
 *
 * Counts verified vs unverified fields from the research brief's verification_flags.
 * Routes below-threshold prospects to quarantine.
 * Deterministic. Zero LLM.
 */

const DEFAULT_CONFIDENCE_THRESHOLD = 0.60;

const REQUIRED_BRIEF_FIELDS = [
  'company_size',
  'funding_stage',
  'estimated_revenue',
  'tech_stack',
  'trigger_signal',
  'prospect_role',
  'prospect_tenure',
  'recent_public_content',
  'crm_history',
  'competitive_context'
];

async function main() {
  const input = await readStdin();
  if (!input) {
    process.stderr.write('No input provided');
    process.exit(1);
  }

  const brief = input.brief || {};
  const verificationFlags = input.verification_flags || brief.verification_flags || [];
  const threshold = input.threshold || DEFAULT_CONFIDENCE_THRESHOLD;
  const domain = input.domain || brief.domain || 'unknown';

  // Count field verification status
  const fieldStatus = REQUIRED_BRIEF_FIELDS.map(field => {
    const flag = verificationFlags.find(f => f.field === field);
    const value = brief[field];

    if (!value && value !== 0 && value !== false) {
      return { field, status: 'missing', source_tier: null };
    }
    if (flag) {
      return { field, status: flag.source_tier, source_tier: flag.source_tier };
    }
    return { field, status: 'unverified', source_tier: null };
  });

  const verified = fieldStatus.filter(f =>
    f.status === 'primary' || f.status === 'secondary'
  ).length;
  const primaryVerified = fieldStatus.filter(f => f.status === 'primary').length;
  const missing = fieldStatus.filter(f => f.status === 'missing').length;
  const unverified = fieldStatus.filter(f => f.status === 'unverified').length;

  const confidence = REQUIRED_BRIEF_FIELDS.length > 0
    ? verified / REQUIRED_BRIEF_FIELDS.length
    : 0;

  const passesThreshold = confidence >= threshold;

  const result = {
    domain,
    confidence: Math.round(confidence * 100) / 100,
    threshold,
    pass: passesThreshold,
    destination: passesThreshold
      ? `briefs/active/${domain}`
      : `briefs/quarantine/${domain}`,
    field_status: fieldStatus,
    summary: {
      total_fields: REQUIRED_BRIEF_FIELDS.length,
      primary_verified: primaryVerified,
      secondary_verified: verified - primaryVerified,
      missing,
      unverified
    },
    remediation: passesThreshold ? null : {
      missing_fields: fieldStatus.filter(f => f.status === 'missing').map(f => f.field),
      unverified_fields: fieldStatus.filter(f => f.status === 'unverified').map(f => f.field),
      options: [
        'Verify missing fields manually',
        'Search alternative data sources',
        'Remove contact from pipeline'
      ]
    },
    timestamp: new Date().toISOString()
  };

  process.stdout.write(JSON.stringify(result));
  process.exit(passesThreshold ? 0 : 1);
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
