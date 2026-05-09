#!/usr/bin/env node
/**
 * scout-match-rate-monitor.js
 * Requirement 34: Flag when email match rate falls below 70%.
 *
 * Deterministic math. Zero LLM.
 */

const MATCH_RATE_THRESHOLD = 0.70;

async function main() {
  const input = await readStdin();
  if (!input) {
    process.stderr.write('No input provided');
    process.exit(1);
  }

  const stats = input.enrichment_stats || {};
  const totalLeads = stats.total_enriched || 0;
  const emailsFound = stats.emails_found || 0;
  const emailsVerified = stats.emails_verified || 0;
  const vertical = stats.vertical || 'unknown';
  const databaseStack = stats.database_stack || [];

  if (totalLeads === 0) {
    process.stdout.write(JSON.stringify({
      pass: true,
      match_rate: null,
      reason: 'No leads enriched in this cycle.',
      timestamp: new Date().toISOString()
    }));
    return;
  }

  const matchRate = emailsVerified / totalLeads;
  const pass = matchRate >= MATCH_RATE_THRESHOLD;

  const result = {
    pass,
    match_rate: Math.round(matchRate * 1000) / 1000,
    threshold: MATCH_RATE_THRESHOLD,
    total_enriched: totalLeads,
    emails_found: emailsFound,
    emails_verified: emailsVerified,
    vertical,
    database_stack: databaseStack,
    severity: pass ? 'ok' : 'warning',
    diagnosis: pass ? null : {
      message: `Email match rate ${(matchRate * 100).toFixed(1)}% is below ${(MATCH_RATE_THRESHOLD * 100)}% threshold.`,
      likely_cause: 'Database routing problem — the selected databases may not have adequate coverage for this vertical.',
      recommendation: `Review database stack for ${vertical} vertical. Current stack: ${databaseStack.join(' → ')}. Consider adding vertical-specific databases or adjusting ICP criteria.`
    },
    timestamp: new Date().toISOString()
  };

  process.stdout.write(JSON.stringify(result));
  process.exit(pass ? 0 : 1);
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
