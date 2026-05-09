#!/usr/bin/env node
/**
 * pitch-compliance-check.js
 * Requirements 4, 5, 6, 7, 26, 30, 41, 42, 43
 *
 * Runs before every outbound message in every pipeline.
 * Six independent hard-stop checks plus re-engagement logic.
 * Deterministic. Zero LLM.
 */

const CSUITE_TITLES = [
  'ceo', 'cto', 'cfo', 'coo', 'president',
  'board member', 'board director', 'chairman', 'chairwoman'
];

const MAX_TOUCHES = 5;

function checkCompliance(prospect, config) {
  const blocks = [];

  // --- Requirement 4: Contact verification confidence ---
  if (!prospect.verification_score && prospect.verification_score !== 0) {
    blocks.push({
      check: 'contact_verification',
      blocked: true,
      reason: 'NO_VERIFICATION_SCORE',
      details: 'Contact has no email verification confidence score from Apollo.',
      remediation: 'Run contact through Apollo email verification before outreach.'
    });
  } else if (prospect.verification_score < (config.min_verification_score || 0.7)) {
    blocks.push({
      check: 'contact_verification',
      blocked: true,
      reason: 'LOW_VERIFICATION_SCORE',
      details: `Verification score ${prospect.verification_score} below threshold ${config.min_verification_score || 0.7}.`,
      remediation: 'Verify email manually or find alternative contact.'
    });
  }

  // --- Requirement 6: GDPR consent for EU prospects ---
  if (prospect.jurisdiction === 'EU' || prospect.jurisdiction === 'EEA' || prospect.jurisdiction === 'UK') {
    if (!prospect.gdpr_consent || !prospect.gdpr_consent.granted) {
      blocks.push({
        check: 'gdpr_consent',
        blocked: true,
        reason: 'GDPR_NO_CONSENT',
        details: `EU/EEA/UK prospect. No GDPR consent on file.`,
        remediation: 'Obtain GDPR consent before any outreach. Record consent with timestamp and source in Attio.'
      });
    }
  }

  // --- Requirement 7: TCPA opt-in for SMS/calls ---
  if (prospect.touch_channel === 'sms' || prospect.touch_channel === 'phone') {
    if (!prospect.tcpa_opt_in || !prospect.tcpa_opt_in.granted) {
      blocks.push({
        check: 'tcpa_opt_in',
        blocked: true,
        reason: 'TCPA_NO_OPT_IN',
        details: `${prospect.touch_channel.toUpperCase()} outreach requires explicit TCPA opt-in. None on file.`,
        remediation: 'Obtain explicit written TCPA consent before SMS or phone outreach. Exposure: $500-$1,500 per message.'
      });
    }
  }

  // --- Requirement 30/41: 5-touch maximum ---
  if (prospect.touch_count >= MAX_TOUCHES) {
    blocks.push({
      check: 'touch_limit',
      blocked: true,
      reason: 'MAX_TOUCHES_REACHED',
      details: `Prospect has received ${prospect.touch_count} touches. 5-touch maximum is an absolute architectural constraint.`,
      remediation: 'Sequence closed. No further outreach permitted. Research documents that sequences exceeding 5 touches produce spam complaints and domain damage.'
    });
  }

  // --- Requirement 41: Opt-out list ---
  if (prospect.opted_out) {
    const optOutDate = new Date(prospect.opt_out_date);
    const now = new Date();
    const daysSinceOptOut = Math.floor((now - optOutDate) / (1000 * 60 * 60 * 24));

    // Requirement 26: Re-engagement logic by jurisdiction
    if (prospect.jurisdiction === 'EU' || prospect.jurisdiction === 'EEA' || prospect.jurisdiction === 'UK') {
      // GDPR: consent withdrawal is permanent unless new consent obtained
      blocks.push({
        check: 'opt_out',
        blocked: true,
        reason: 'GDPR_CONSENT_WITHDRAWN',
        details: 'GDPR consent withdrawal is permanent. Cannot re-engage without new explicit consent.',
        remediation: 'Obtain fresh GDPR consent and record in Attio before any re-engagement.'
      });
    } else if (daysSinceOptOut < (config.opt_out_window_days || 30)) {
      // CAN-SPAM: within opt-out window
      blocks.push({
        check: 'opt_out',
        blocked: true,
        reason: 'OPT_OUT_ACTIVE',
        details: `Contact opted out ${daysSinceOptOut} days ago. Window: ${config.opt_out_window_days || 30} days.`,
        remediation: `Wait ${(config.opt_out_window_days || 30) - daysSinceOptOut} more days before re-engagement is possible.`
      });
    } else {
      // CAN-SPAM: window expired — route to approval, don't hard block
      blocks.push({
        check: 'opt_out',
        blocked: false,
        requires_approval: true,
        reason: 'OPT_OUT_WINDOW_EXPIRED',
        details: `Contact opted out ${daysSinceOptOut} days ago. Opt-out window has expired. Re-engagement requires rep approval.`,
        remediation: 'Rep must explicitly authorize re-engagement. Authorization will be logged in audit trail.'
      });
    }
  }

  // --- Requirement 41: Explicit decline ---
  if (prospect.explicitly_declined && !prospect.opted_out) {
    blocks.push({
      check: 'explicit_decline',
      blocked: true,
      reason: 'EXPLICIT_DECLINE',
      details: 'Prospect explicitly asked not to be contacted.',
      remediation: 'Respect the decline. Do not outreach regardless of signal strength.'
    });
  }

  // --- Requirement 41: Current customer without authorization ---
  if (prospect.is_current_customer && !prospect.customer_outreach_authorized) {
    blocks.push({
      check: 'current_customer',
      blocked: true,
      reason: 'CURRENT_CUSTOMER',
      details: 'Contact is flagged as a current customer in CRM. Sales outreach requires explicit authorization.',
      remediation: 'Get rep authorization for customer outreach. This protects existing relationships from unsolicited sales contact.'
    });
  }

  // --- Result ---
  const hardBlocks = blocks.filter(b => b.blocked);
  const approvalRequired = blocks.filter(b => !b.blocked && b.requires_approval);

  return {
    pass: hardBlocks.length === 0,
    requires_approval: approvalRequired.length > 0,
    blocks: hardBlocks,
    approval_needed: approvalRequired,
    prospect_domain: prospect.domain,
    timestamp: new Date().toISOString()
  };
}

// --- Requirement 24: C-suite check (used by sequence scheduler) ---
function isCsuite(title) {
  if (!title) return false;
  const lower = title.toLowerCase();
  return CSUITE_TITLES.some(t => lower.includes(t));
}

async function main() {
  const input = await readStdin();
  if (!input) {
    process.stderr.write('No input provided');
    process.exit(1);
  }

  const prospect = input.prospect || input;
  const config = input.config || {};

  const result = checkCompliance(prospect, config);

  // Attach C-suite flag for downstream routing (Requirement 24)
  result.is_csuite = isCsuite(prospect.title);

  process.stdout.write(JSON.stringify(result));
  process.exit(result.pass ? 0 : 1);
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

module.exports = { checkCompliance, isCsuite };

if (require.main === module) {
  main().catch(err => {
    process.stderr.write(err.message);
    process.exit(1);
  });
}
