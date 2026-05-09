#!/usr/bin/env node
/**
 * scout-dossier-builder.js
 * Requirements 42, 43, 51: Complete dossier with all required fields.
 *
 * Assembles enrichment, scoring, signal, and stakeholder data into handoff package.
 * Common signal-to-angle mappings are deterministic.
 * Only unusual signals need llm-task for outreach angle.
 * 
 * Deterministic assembly for ~70% of leads. Zero LLM for those.
 */

const SIGNAL_ANGLE_MAP = {
  funding: 'Recently raised capital and evaluating tools to deploy it.',
  series_a: 'Just closed Series A — building foundational infrastructure.',
  series_b: 'Series B closed — scaling operations and team.',
  series_c: 'Series C closed — optimizing for efficiency and market expansion.',
  job_posting_sales: 'Building a sales team — likely needs sales enablement infrastructure.',
  job_posting_engineering: 'Expanding engineering — investing in development tools and infrastructure.',
  job_posting_data: 'Hiring data roles — building data infrastructure.',
  job_posting_revops: 'Hiring Revenue Operations — investing in GTM stack.',
  job_posting_marketing: 'Growing marketing team — likely evaluating marketing automation.',
  competitor_displacement: 'Expressed frustration with current solution — potential switch opportunity.',
  competitor_negative_review: 'Left negative review of competitor — actively dissatisfied.',
  website_visit_pricing: 'Visited pricing page — actively evaluating your product.',
  website_visit_demo: 'Visited demo page — high buying intent.',
  website_visit_comparison: 'Viewed comparison page — evaluating alternatives.',
  tech_stack_integration: 'Running tools your product integrates with — natural expansion.',
  tech_stack_competitor: 'Running a competitor product — displacement opportunity.',
  leadership_change: 'New leadership — likely reviewing and changing vendor stack.',
  acquisition: 'Recent acquisition — infrastructure consolidation creates buying window.',
  regulatory_change: 'Regulatory change in their space — may need compliance tooling.',
  reddit_pain: 'Publicly expressed a pain point your product addresses.',
  intent_bombora: 'Actively researching your product category.',
  intent_6sense: 'Identified as in-market by behavioral signals.',
  crm_reengagement: 'Previously engaged prospect showing renewed interest.'
};

function getOutreachAngle(signalType, signalContext) {
  const normalized = (signalType || '').toLowerCase().replace(/[\s-]+/g, '_');

  // Check deterministic map first
  for (const [key, angle] of Object.entries(SIGNAL_ANGLE_MAP)) {
    if (normalized.includes(key) || key.includes(normalized)) {
      return { angle, source: 'deterministic', needs_llm: false };
    }
  }

  // If no match, flag for llm-task
  return {
    angle: null,
    source: 'needs_llm',
    needs_llm: true,
    signal_type: signalType,
    signal_context: signalContext
  };
}

function buildDossier(lead, enrichment, scoring, signal, stakeholders) {
  const angle = getOutreachAngle(signal.type, signal.context);

  const dossier = {
    // Contact profile
    contact: {
      email: enrichment.email || null,
      email_verified: enrichment._sources?.email ? true : false,
      direct_dial: enrichment.direct_dial || null,
      title: enrichment.title || null,
      seniority: enrichment.seniority || null,
      linkedin_url: enrichment.linkedin_url || null,
      name: lead.name || enrichment.name || null
    },

    // Company profile
    company: {
      name: enrichment.company_name || lead.company_name || null,
      domain: lead.domain || enrichment.company_domain || null,
      size: enrichment.company_size || null,
      revenue_range: enrichment.revenue_range || null,
      industry: enrichment.industry || null,
      founding_year: enrichment.founding_year || null,
      funding_stage: enrichment.funding_stage || null,
      tech_stack: enrichment.tech_stack || [],
      recent_news: enrichment.recent_news || null
    },

    // Signal that triggered sourcing
    trigger_signal: {
      type: signal.type,
      source: signal.source,
      date: signal.date || signal.timestamp,
      summary: signal.summary || signal.context,
      raw_data: signal.raw || null
    },

    // ICP score with breakdown
    score: {
      composite: scoring.score,
      tier: scoring.tier,
      dimensions: scoring.dimensions,
      exclusivity_factor: scoring.exclusivity_factor,
      visitor_boost: scoring.visitor_boost || 0
    },

    // Outreach angle
    outreach_angle: angle.angle,
    outreach_angle_source: angle.source,
    outreach_angle_needs_llm: angle.needs_llm,

    // Urgency flag
    urgency: scoring.urgency || false,
    urgency_reason: scoring.urgency
      ? (signal.type === 'visitor_identification'
        ? 'Website visitor — highest urgency signal'
        : 'Score 90+ — time-sensitive opportunity')
      : null,

    // Additional stakeholders (Req 31)
    stakeholders: (stakeholders || []).map(s => ({
      name: s.name || null,
      title: s.title || null,
      email: s.email || null,
      linkedin_url: s.linkedin_url || null,
      role_type: s.role_type, // decision_maker, economic_buyer, champion
      _source: s._source || null
    })),

    // Data provenance (Req 51)
    data_sources: enrichment._sources || {},

    // Metadata
    brief_id: lead.brief_id || null,
    sourced_at: new Date().toISOString()
  };

  // Validate completeness (Req 43)
  const requiredFields = [
    'contact.email', 'company.domain', 'trigger_signal.type',
    'trigger_signal.summary', 'score.composite', 'score.tier'
  ];
  const missing = requiredFields.filter(path => {
    const parts = path.split('.');
    let val = dossier;
    for (const p of parts) val = val?.[p];
    return val === null || val === undefined;
  });

  dossier.complete = missing.length === 0;
  dossier.missing_required = missing;

  return dossier;
}

async function main() {
  const input = await readStdin();
  if (!input) {
    process.stderr.write('No input provided');
    process.exit(1);
  }

  const dossier = buildDossier(
    input.lead || {},
    input.enrichment || {},
    input.scoring || {},
    input.signal || {},
    input.stakeholders || []
  );

  process.stdout.write(JSON.stringify(dossier));
  process.exit(dossier.complete ? 0 : 1);
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

module.exports = { buildDossier, getOutreachAngle, SIGNAL_ANGLE_MAP };

if (require.main === module) {
  main().catch(err => {
    process.stderr.write(err.message);
    process.exit(1);
  });
}
