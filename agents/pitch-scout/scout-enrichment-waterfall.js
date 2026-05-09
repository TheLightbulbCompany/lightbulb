#!/usr/bin/env node
/**
 * scout-enrichment-waterfall.js
 * Requirements 29, 30, 31, 32, 33, 51: Waterfall enrichment with field-level tracking.
 *
 * Orchestrates API calls in vertical-router-specified order.
 * Tracks each field independently — stops waterfall per-field when populated.
 * Tags every field with _source for audit traceability.
 * Deterministic orchestration. Zero LLM.
 */

const REQUIRED_FIELDS = [
  'email', 'direct_dial', 'title', 'seniority', 'linkedin_url',
  'company_name', 'company_size', 'revenue_range', 'industry',
  'founding_year', 'funding_stage', 'tech_stack', 'recent_news'
];

// Role mapping for multi-contact identification (Req 31)
const ROLE_EXPANSIONS = {
  'head of sales': {
    decision_maker: ['vp sales', 'head of sales', 'director of sales'],
    economic_buyer: ['cro', 'ceo', 'cfo'],
    champion: ['sales manager', 'senior account executive', 'sales operations manager']
  },
  'head of engineering': {
    decision_maker: ['vp engineering', 'head of engineering', 'director of engineering'],
    economic_buyer: ['cto', 'ceo', 'vp technology'],
    champion: ['staff engineer', 'tech lead', 'engineering manager']
  },
  'head of marketing': {
    decision_maker: ['vp marketing', 'head of marketing', 'cmo', 'director of marketing'],
    economic_buyer: ['cro', 'ceo', 'cfo'],
    champion: ['marketing manager', 'growth lead', 'demand gen manager']
  },
  'head of operations': {
    decision_maker: ['vp operations', 'head of operations', 'coo', 'director of operations'],
    economic_buyer: ['ceo', 'cfo'],
    champion: ['operations manager', 'process improvement lead']
  },
  'head of hr': {
    decision_maker: ['vp hr', 'head of hr', 'chro', 'director of hr', 'vp people'],
    economic_buyer: ['ceo', 'cfo', 'coo'],
    champion: ['hr manager', 'people operations manager', 'talent acquisition lead']
  },
  'default': {
    decision_maker: ['vp', 'director', 'head of'],
    economic_buyer: ['ceo', 'cfo', 'coo', 'cto'],
    champion: ['manager', 'senior', 'lead']
  }
};

function expandRoles(targetRole) {
  const normalized = (targetRole || '').toLowerCase();
  for (const [key, expansion] of Object.entries(ROLE_EXPANSIONS)) {
    if (key === 'default') continue;
    if (normalized.includes(key) || key.includes(normalized)) {
      return expansion;
    }
  }
  return ROLE_EXPANSIONS.default;
}

function mergeEnrichment(existing, newData, sourceName) {
  const merged = { ...existing };
  const sources = { ...(existing._sources || {}) };

  for (const field of REQUIRED_FIELDS) {
    // Only populate if field is empty (waterfall stops per-field)
    if (!merged[field] && newData[field]) {
      merged[field] = newData[field];
      sources[field] = {
        source: sourceName,
        retrieved_at: new Date().toISOString()
      };
    }
  }

  // Special handling for arrays (tech_stack, recent_news)
  if (Array.isArray(newData.tech_stack) && (!merged.tech_stack || merged.tech_stack.length === 0)) {
    merged.tech_stack = newData.tech_stack;
    sources.tech_stack = { source: sourceName, retrieved_at: new Date().toISOString() };
  }

  merged._sources = sources;
  return merged;
}

function checkCompleteness(enriched) {
  const populated = REQUIRED_FIELDS.filter(f => {
    const val = enriched[f];
    if (Array.isArray(val)) return val.length > 0;
    return val !== null && val !== undefined && val !== '';
  });

  return {
    total_fields: REQUIRED_FIELDS.length,
    populated: populated.length,
    missing: REQUIRED_FIELDS.filter(f => !populated.includes(f)),
    completeness: Math.round((populated.length / REQUIRED_FIELDS.length) * 100) / 100
  };
}

async function main() {
  const input = await readStdin();
  if (!input) {
    process.stderr.write('No input provided');
    process.exit(1);
  }

  const lead = input.lead || {};
  const databaseStack = input.database_stack || [];
  const targetRole = input.target_role || '';
  const cachedProfile = input.cached_profile || null;

  // Start with cached data if available (Req 33)
  let enriched = cachedProfile ? { ...cachedProfile } : {};
  enriched._sources = enriched._sources || {};

  // Track which databases were queried
  const queriedDatabases = [];

  // The actual API calls happen in the Lobster pipeline — this script
  // receives the results from each database and merges them
  const databaseResults = input.database_results || [];

  for (const result of databaseResults) {
    enriched = mergeEnrichment(enriched, result.data, result.source);
    queriedDatabases.push({
      name: result.source,
      fields_added: Object.keys(result.data).filter(k =>
        enriched._sources[k]?.source === result.source
      ),
      type: result.type
    });
  }

  // Completeness check
  const completeness = checkCompleteness(enriched);

  // Role expansion for multi-contact (Req 31)
  const roleExpansion = expandRoles(targetRole);

  const output = {
    enriched_profile: enriched,
    data_sources: enriched._sources,
    completeness,
    role_expansion: roleExpansion,
    target_role: targetRole,
    databases_queried: queriedDatabases,
    fields_unverified: completeness.missing,
    domain: lead.domain || enriched.company_domain,
    timestamp: new Date().toISOString()
  };

  process.stdout.write(JSON.stringify(output));
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

module.exports = { mergeEnrichment, checkCompleteness, expandRoles, REQUIRED_FIELDS };

if (require.main === module) {
  main().catch(err => {
    process.stderr.write(err.message);
    process.exit(1);
  });
}
