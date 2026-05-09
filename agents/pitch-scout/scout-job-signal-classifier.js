#!/usr/bin/env node
/**
 * scout-job-signal-classifier.js
 * Requirement 21: Interpret job postings as intent signals.
 *
 * Deterministic keyword classification handles ~70% of postings.
 * Unclassified postings flagged for llm-task.
 * Zero LLM for classified postings.
 */

const JOB_INTENT_PATTERNS = [
  {
    keywords: ['sdr', 'bdr', 'sales development', 'business development rep', 'outbound sales'],
    intent: 'building_sales_team',
    angle: 'Building outbound sales team — likely needs sales enablement infrastructure.',
    category: 'job_posting_sales'
  },
  {
    keywords: ['account executive', 'ae ', 'closing rep', 'enterprise sales'],
    intent: 'scaling_sales',
    angle: 'Scaling sales organization — evaluating tools to support enterprise selling.',
    category: 'job_posting_sales'
  },
  {
    keywords: ['revenue operations', 'revops', 'sales operations', 'gtm operations'],
    intent: 'gtm_stack_investment',
    angle: 'Investing in GTM operations — evaluating revenue stack tooling.',
    category: 'job_posting_revops'
  },
  {
    keywords: ['data engineer', 'data scientist', 'ml engineer', 'machine learning', 'data infrastructure'],
    intent: 'data_infrastructure',
    angle: 'Building data infrastructure — investing in data tools and platforms.',
    category: 'job_posting_data'
  },
  {
    keywords: ['head of engineering', 'vp engineering', 'director of engineering', 'engineering manager'],
    intent: 'engineering_leadership',
    angle: 'Hiring engineering leadership — likely reviewing and selecting dev tools and infrastructure.',
    category: 'job_posting_engineering'
  },
  {
    keywords: ['devops', 'site reliability', 'sre', 'platform engineer', 'infrastructure engineer'],
    intent: 'devops_investment',
    angle: 'Investing in DevOps/platform engineering — evaluating infrastructure tooling.',
    category: 'job_posting_engineering'
  },
  {
    keywords: ['head of marketing', 'vp marketing', 'director of marketing', 'cmo', 'growth marketing'],
    intent: 'marketing_leadership',
    angle: 'Hiring marketing leadership — likely evaluating marketing automation and analytics.',
    category: 'job_posting_marketing'
  },
  {
    keywords: ['demand gen', 'demand generation', 'content marketing', 'product marketing'],
    intent: 'marketing_scaling',
    angle: 'Scaling marketing operations — investing in demand generation tools.',
    category: 'job_posting_marketing'
  },
  {
    keywords: ['customer success', 'csm', 'customer experience', 'cx manager'],
    intent: 'customer_success',
    angle: 'Building customer success function — evaluating CS platforms and tools.',
    category: 'job_posting_cs'
  },
  {
    keywords: ['head of hr', 'vp people', 'people operations', 'chro', 'talent acquisition'],
    intent: 'hr_investment',
    angle: 'Investing in people operations — evaluating HR technology.',
    category: 'job_posting_hr'
  },
  {
    keywords: ['security engineer', 'ciso', 'head of security', 'infosec', 'application security'],
    intent: 'security_investment',
    angle: 'Building security function — evaluating security tools and compliance platforms.',
    category: 'job_posting_security'
  },
  {
    keywords: ['product manager', 'head of product', 'vp product', 'director of product'],
    intent: 'product_leadership',
    angle: 'Hiring product leadership — likely evaluating product management and analytics tools.',
    category: 'job_posting_product'
  }
];

function classifyJobPosting(posting) {
  const title = (posting.title || '').toLowerCase();
  const description = (posting.description || '').toLowerCase();
  const fullText = `${title} ${description}`;

  for (const pattern of JOB_INTENT_PATTERNS) {
    const match = pattern.keywords.some(kw => fullText.includes(kw));
    if (match) {
      return {
        classified: true,
        needs_llm: false,
        intent: pattern.intent,
        angle: pattern.angle,
        category: pattern.category,
        matched_keywords: pattern.keywords.filter(kw => fullText.includes(kw)),
        company: posting.company,
        domain: posting.domain
      };
    }
  }

  // Count of same-title postings indicates team building
  if (posting.similar_posting_count && posting.similar_posting_count >= 3) {
    return {
      classified: true,
      needs_llm: false,
      intent: 'team_scaling',
      angle: `Posting ${posting.similar_posting_count} similar roles — rapidly scaling this function.`,
      category: 'job_posting_scaling',
      company: posting.company,
      domain: posting.domain
    };
  }

  // Unclassified — needs llm-task
  return {
    classified: false,
    needs_llm: true,
    title: posting.title,
    description: (posting.description || '').substring(0, 500),
    company: posting.company,
    domain: posting.domain
  };
}

async function main() {
  const input = await readStdin();
  if (!input) {
    process.stderr.write('No input provided');
    process.exit(1);
  }

  const postings = input.postings || [input];
  const classified = [];
  const needsLlm = [];

  for (const posting of postings) {
    const result = classifyJobPosting(posting);
    if (result.classified) {
      classified.push(result);
    } else {
      needsLlm.push(result);
    }
  }

  const output = {
    classified,
    needs_llm: needsLlm,
    total: postings.length,
    classified_count: classified.length,
    needs_llm_count: needsLlm.length,
    classification_rate: Math.round((classified.length / postings.length) * 100) / 100,
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

module.exports = { classifyJobPosting, JOB_INTENT_PATTERNS };

if (require.main === module) {
  main().catch(err => {
    process.stderr.write(err.message);
    process.exit(1);
  });
}
