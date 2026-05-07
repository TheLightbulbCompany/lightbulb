#!/usr/bin/env node
/**
 * scout-vertical-router.js
 * Requirements 7-18: Select database stack based on inferred vertical.
 * 
 * Deterministic lookup table. Zero LLM.
 * Every database is conditional — only used if customer has it configured.
 */

const VERTICAL_STACKS = {
  technology: {
    label: 'Technology / SaaS',
    databases: [
      { name: 'apollo', type: 'skill', primary: true, fields: ['email', 'title', 'seniority', 'linkedin', 'company', 'size', 'revenue', 'industry', 'founding_year', 'funding', 'tech_stack'] },
      { name: 'zoominfo', type: 'direct_api', primary: false, fields: ['email', 'direct_dial', 'title', 'seniority', 'company'] },
      { name: 'cognism', type: 'direct_api', primary: false, conditional: true, fields: ['email', 'direct_dial', 'title', 'company'], region: 'EMEA' }
    ],
    signal_sources: {
      jobs: ['greenhouse', 'lever', 'ashby'],
      technographic: ['builtwith', 'wappalyzer', 'hg_insights'],
      funding: ['apollo', 'pitchbook', 'techcrunch'],
      intent: ['bombora', '6sense'],
      community: ['reddit', 'hackernews'],
      news: ['perplexity'],
      reviews: ['g2', 'capterra', 'trustradius']
    },
    review_sites: ['g2.com', 'capterra.com', 'trustradius.com']
  },

  finance: {
    label: 'Finance / Fintech',
    databases: [
      { name: 'zoominfo', type: 'direct_api', primary: true, vertical_filter: 'financial_services', fields: ['email', 'direct_dial', 'title', 'seniority', 'company'] },
      { name: 'refinitiv', type: 'direct_api', primary: false, fields: ['company', 'contacts', 'institutional_data'] },
      { name: 'pitchbook', type: 'direct_api', primary: false, conditional: true, fields: ['pe_contacts', 'vc_contacts', 'fund_data'] }
    ],
    signal_sources: {
      regulatory: ['sec_edgar'],
      funding: ['apollo', 'pitchbook'],
      intent: ['bombora', '6sense'],
      news: ['perplexity'],
      reviews: ['g2.com/categories/financial-services']
    },
    review_sites: ['g2.com', 'capterra.com']
  },

  healthcare: {
    label: 'Healthcare / MedTech',
    databases: [
      { name: 'definitive_healthcare', type: 'direct_api', primary: true, conditional: true, fields: ['hospital_contacts', 'physician_data', 'affiliation', 'company'] },
      { name: 'zoominfo', type: 'direct_api', primary: false, vertical_filter: 'healthcare', fields: ['email', 'direct_dial', 'title', 'company'] },
      { name: 'iqvia', type: 'direct_api', primary: false, conditional: true, fields: ['pharma_contacts', 'biotech_contacts'] }
    ],
    signal_sources: {
      regulatory: ['cms_gov', 'fda_clearances'],
      funding: ['apollo'],
      intent: ['bombora', '6sense'],
      news: ['perplexity'],
      reviews: ['g2.com', 'klas_research']
    },
    review_sites: ['g2.com', 'klasresearch.com']
  },

  legal: {
    label: 'Legal',
    databases: [
      { name: 'martindale_hubbell', type: 'agent_browser', primary: true, conditional: true, fields: ['attorney_contacts', 'firm_data', 'practice_areas'] },
      { name: 'bloomberg_law', type: 'direct_api', primary: false, conditional: true, fields: ['firm_contacts', 'litigation_data'] },
      { name: 'zoominfo', type: 'direct_api', primary: false, vertical_filter: 'legal', fields: ['email', 'direct_dial', 'title', 'company'] }
    ],
    signal_sources: {
      lateral_hires: ['perplexity', 'agent_browser'],
      news: ['perplexity'],
      reviews: ['capterra_legal', 'legal_it_insider'],
      intent: ['bombora']
    },
    review_sites: ['capterra.com/legal', 'legalitinsider.com']
  },

  real_estate: {
    label: 'Real Estate / PropTech',
    databases: [
      { name: 'costar', type: 'direct_api', primary: true, conditional: true, fields: ['property_contacts', 'ownership', 'transactions'] },
      { name: 'reonomy', type: 'direct_api', primary: false, conditional: true, fields: ['property_data', 'owner_contacts'] },
      { name: 'zoominfo', type: 'direct_api', primary: false, vertical_filter: 'real_estate', fields: ['email', 'direct_dial', 'title', 'company'] }
    ],
    signal_sources: {
      transactions: ['agent_browser'],
      permits: ['agent_browser'],
      news: ['perplexity'],
      intent: ['bombora']
    },
    review_sites: ['g2.com', 'capterra.com']
  },

  manufacturing: {
    label: 'Manufacturing / Industrial',
    databases: [
      { name: 'thomasnet', type: 'agent_browser', primary: true, fields: ['industrial_contacts', 'company', 'products', 'certifications'] },
      { name: 'dnb', type: 'direct_api', primary: false, fields: ['company', 'supply_chain', 'firmographics'] },
      { name: 'zoominfo', type: 'direct_api', primary: false, vertical_filter: 'manufacturing', fields: ['email', 'direct_dial', 'title', 'company'] }
    ],
    signal_sources: {
      regulatory: ['epa_gov', 'osha_gov'],
      trade: ['perplexity'],
      news: ['perplexity'],
      intent: ['bombora']
    },
    review_sites: ['g2.com', 'capterra.com']
  },

  retail: {
    label: 'Retail / eCommerce',
    databases: [
      { name: 'builtwith', type: 'direct_api', primary: true, conditional: true, fields: ['tech_stack', 'ecommerce_platform', 'growth'] },
      { name: 'apollo', type: 'skill', primary: false, fields: ['email', 'title', 'seniority', 'company'] }
    ],
    signal_sources: {
      seasonal: ['perplexity'],
      technographic: ['builtwith', 'wappalyzer'],
      news: ['perplexity'],
      intent: ['bombora', '6sense']
    },
    review_sites: ['g2.com', 'capterra.com', 'trustpilot.com']
  },

  professional_services: {
    label: 'Professional Services',
    databases: [
      { name: 'zoominfo', type: 'direct_api', primary: true, fields: ['email', 'direct_dial', 'title', 'seniority', 'company'] },
      { name: 'dnb', type: 'direct_api', primary: false, fields: ['company', 'firmographics', 'financials'] }
    ],
    signal_sources: {
      growth: ['perplexity'],
      news: ['perplexity'],
      intent: ['bombora']
    },
    review_sites: ['g2.com', 'capterra.com', 'clutch.co']
  },

  nonprofit: {
    label: 'Nonprofit / Education',
    databases: [
      { name: 'candid', type: 'direct_api', primary: true, conditional: true, fields: ['nonprofit_contacts', 'financials', 'leadership', 'mission'] },
      { name: 'linkedin', type: 'agent_browser', primary: false, fields: ['contacts', 'company'], filters: 'nonprofit_sector' }
    ],
    signal_sources: {
      filings: ['candid_api', 'irs_990'],
      grants: ['perplexity'],
      news: ['perplexity']
    },
    review_sites: []
  },

  government: {
    label: 'Government / Public Sector',
    databases: [
      { name: 'sam_gov', type: 'direct_api', primary: true, fields: ['entities', 'contract_vehicles', 'contacts'] },
      { name: 'govwin', type: 'agent_browser', primary: false, conditional: true, fields: ['opportunities', 'contacts', 'budgets'] },
      { name: 'zoominfo', type: 'direct_api', primary: false, vertical_filter: 'government', fields: ['email', 'direct_dial', 'title', 'company'] }
    ],
    signal_sources: {
      contracts: ['sam_gov_api', 'fpds_gov'],
      procurement: ['agent_browser'],
      news: ['perplexity']
    },
    review_sites: []
  }
};

// Universal enrichment: Apollo is primary (in skills[]), ZoomInfo is optional enterprise click-to-connect.
// Waterfall: Apollo → (optional ZoomInfo if connected) → done. Email verification via Apollo built-in.
const UNIVERSAL_ENRICHMENT = [
  { name: 'zoominfo', type: 'direct_api', conditional: true, fields: ['email', 'direct_dial', 'title', 'seniority', 'company'] }
];

function routeVertical(vertical, configuredDatabases) {
  const normalizedVertical = vertical.toLowerCase().replace(/[\s\/]+/g, '_');

  // Find matching stack
  let stack = VERTICAL_STACKS[normalizedVertical];

  // Try partial matches
  if (!stack) {
    const keys = Object.keys(VERTICAL_STACKS);
    const match = keys.find(k =>
      normalizedVertical.includes(k) || k.includes(normalizedVertical)
    );
    if (match) stack = VERTICAL_STACKS[match];
  }

  if (!stack) {
    // Escape hatch: unknown vertical routes to agent loop for judgment
    // instead of forcing a technology fallback that may be wrong.
    // The agent loop can construct a custom database stack from available
    // configured databases, or ask the user for clarification.
    return {
      error: false,
      needs_agent_loop: true,
      message: `Vertical "${vertical}" doesn't match the 10 predefined categories. The agent loop should determine the right database stack based on the user's ICP description and available databases.`,
      vertical: normalizedVertical,
      available_verticals: Object.keys(VERTICAL_STACKS),
      // Provide universal databases as a starting point
      databases: UNIVERSAL_ENRICHMENT.map(db => ({
        ...db,
        available: (configuredDatabases || []).includes(db.name) || db.always_run
      })),
      configured_databases: configuredDatabases || [],
      signal_sources: { news: ['perplexity'], intent: ['bombora', '6sense'] }
    };
  }

  // Filter databases by what's actually configured
  const configured = configuredDatabases || [];
  const availableDatabases = stack.databases.map(db => ({
    ...db,
    available: !db.conditional || configured.includes(db.name)
  }));

  // Add universal enrichment sources
  const fullStack = [
    ...availableDatabases,
    ...UNIVERSAL_ENRICHMENT.map(db => ({
      ...db,
      available: configured.includes(db.name) || db.always_run
    }))
  ];

  return {
    error: false,
    vertical: normalizedVertical,
    label: stack.label,
    databases: fullStack,
    available_databases: fullStack.filter(db => db.available),
    unavailable_databases: fullStack.filter(db => !db.available),
    signal_sources: stack.signal_sources,
    review_sites: stack.review_sites
  };
}

async function main() {
  const input = await readStdin();
  if (!input) {
    process.stderr.write('No input provided');
    process.exit(1);
  }

  const vertical = input.vertical;
  const configuredDatabases = input.configured_databases || [];

  if (!vertical) {
    process.stderr.write('vertical field is required');
    process.exit(1);
  }

  const result = routeVertical(vertical, configuredDatabases);
  result.timestamp = new Date().toISOString();

  process.stdout.write(JSON.stringify(result));
  process.exit(result.error ? 1 : 0);
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

module.exports = { routeVertical, VERTICAL_STACKS, UNIVERSAL_ENRICHMENT };

if (require.main === module) {
  main().catch(err => {
    process.stderr.write(err.message);
    process.exit(1);
  });
}
