#!/usr/bin/env node
/**
 * scout-compliance-check.js
 * Requirements 48, 50, 62: Business email only, high-restriction region flagging.
 *
 * Deterministic checks. Zero LLM.
 */

const PERSONAL_EMAIL_DOMAINS = [
  'gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com', 'aol.com',
  'icloud.com', 'mail.com', 'protonmail.com', 'zoho.com', 'yandex.com',
  'live.com', 'msn.com', 'me.com', 'mac.com', 'inbox.com',
  'gmx.com', 'gmx.net', 'fastmail.com', 'hey.com', 'tutanota.com',
  'yahoo.co.uk', 'yahoo.co.jp', 'hotmail.co.uk', 'outlook.co.uk',
  'googlemail.com', 'qq.com', '163.com', '126.com', 'sina.com'
];

const HIGH_RESTRICTION_REGIONS = {
  'canada': { law: 'CASL', note: 'Express consent required before commercial electronic messages. Implied consent limited to 2 years from business relationship.' },
  'ca': { law: 'CASL', note: 'Express consent required before commercial electronic messages.' },
  'germany': { law: 'GDPR + UWG', note: 'Germany has the strictest GDPR enforcement in the EU. Cold email without prior consent is prohibited under UWG (Unfair Competition Act).' },
  'de': { law: 'GDPR + UWG', note: 'Strictest GDPR enforcement. Cold B2B email heavily restricted.' },
  'australia': { law: 'Spam Act 2003', note: 'Requires consent or existing business relationship. No purchased list outreach.' },
  'au': { law: 'Spam Act 2003', note: 'Consent required. Penalties up to AUD 2.1M per day.' }
};

function checkCompliance(lead) {
  const flags = [];

  // Requirement 48: Business email only
  const email = (lead.email || '').toLowerCase().trim();
  if (email) {
    const domain = email.split('@')[1];
    if (domain && PERSONAL_EMAIL_DOMAINS.includes(domain)) {
      flags.push({
        type: 'personal_email',
        severity: 'block',
        field: 'email',
        value: email,
        action: 'strip_email',
        reason: `Personal email domain detected (${domain}). Only business email addresses are sourced. Email stripped from dossier.`
      });
    }
  }

  // Requirement 50: High-restriction region flagging
  const geography = (lead.geography || lead.country || '').toLowerCase().trim();
  for (const [region, info] of Object.entries(HIGH_RESTRICTION_REGIONS)) {
    if (geography.includes(region) || geography === region) {
      flags.push({
        type: 'high_restriction_region',
        severity: 'flag',
        region: geography,
        law: info.law,
        note: info.note,
        action: 'flag_in_dossier',
        reason: `Contact in high-restriction cold outreach region (${info.law}). Outreach carries elevated legal risk — user should confirm before proceeding.`
      });
      break;
    }
  }

  const blocked = flags.some(f => f.severity === 'block');
  const flagged = flags.some(f => f.severity === 'flag');

  return {
    pass: !blocked,
    has_flags: flags.length > 0,
    flags,
    action: blocked ? 'strip_and_continue' : flagged ? 'flag_and_continue' : 'proceed',
    lead_domain: lead.domain || 'unknown',
    timestamp: new Date().toISOString()
  };
}

async function main() {
  const input = await readStdin();
  if (!input) {
    process.stderr.write('No input provided');
    process.exit(1);
  }

  const leads = input.leads || [input.lead || input];
  const results = leads.map(lead => checkCompliance(lead));

  const output = {
    results,
    total: results.length,
    blocked: results.filter(r => !r.pass).length,
    flagged: results.filter(r => r.has_flags && r.pass).length,
    clean: results.filter(r => !r.has_flags).length,
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

module.exports = { checkCompliance, PERSONAL_EMAIL_DOMAINS, HIGH_RESTRICTION_REGIONS };

if (require.main === module) {
  main().catch(err => {
    process.stderr.write(err.message);
    process.exit(1);
  });
}
