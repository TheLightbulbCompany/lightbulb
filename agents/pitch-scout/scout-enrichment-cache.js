#!/usr/bin/env node
/**
 * scout-enrichment-cache.js
 * Requirement 33: Cache enrichment per domain for 30 days.
 *
 * Deterministic timestamp comparison. Zero LLM.
 */

const CACHE_TTL_DAYS = 30;
const TIME_SENSITIVE_FIELDS = ['recent_news', 'funding_stage', 'tech_stack'];

async function main() {
  const input = await readStdin();
  if (!input) {
    process.stderr.write('No input provided');
    process.exit(1);
  }

  const domain = input.domain;
  const cachedProfile = input.cached_profile || null;

  if (!cachedProfile) {
    process.stdout.write(JSON.stringify({
      cache_hit: false,
      domain,
      action: 'full_enrichment',
      reason: 'No cached profile found.'
    }));
    return;
  }

  const cachedAt = new Date(cachedProfile.cached_at || cachedProfile.timestamp);
  const ageInDays = (Date.now() - cachedAt.getTime()) / (1000 * 60 * 60 * 24);

  if (ageInDays > CACHE_TTL_DAYS) {
    process.stdout.write(JSON.stringify({
      cache_hit: false,
      domain,
      action: 'full_enrichment',
      reason: `Cache expired. Age: ${Math.round(ageInDays)} days. TTL: ${CACHE_TTL_DAYS} days.`
    }));
    return;
  }

  // Cache is valid — use it, but flag time-sensitive fields for refresh
  process.stdout.write(JSON.stringify({
    cache_hit: true,
    domain,
    action: 'partial_refresh',
    cached_profile: cachedProfile,
    cache_age_days: Math.round(ageInDays),
    refresh_fields: TIME_SENSITIVE_FIELDS,
    reason: `Cache valid. Age: ${Math.round(ageInDays)} days. Refreshing time-sensitive fields only.`
  }));
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
