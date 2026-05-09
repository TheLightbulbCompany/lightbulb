#!/usr/bin/env node
/**
 * pitch-activation-check.js
 * Requirement 1: ICP must be explicitly defined before any outreach.
 * Requirement 2: Bounce rate must be below threshold.
 * Requirement 3: Test cohort phase enforcement.
 * Requirement 5: CAN-SPAM config validation (outbound platform template check).
 *
 * Runs on first activation and as step zero in outreach pipelines.
 * Deterministic. Zero LLM.
 */

const REQUIRED_ICP_FIELDS = [
  'company_size_min',
  'company_size_max',
  'funding_stages',
  'geographies',
  'industries',
  'tech_stack_signals',
  'revenue_min'
];

const BOUNCE_RATE_THRESHOLD = 0.10;
const TEST_COHORT_MIN = 50;
const TEST_COHORT_MAX = 100;

async function main() {
  const input = await readStdin();
  const checks = [];

  // --- Requirement 1: ICP Definition Exists ---
  const icpConfig = input?.icp_config || null;
  if (!icpConfig) {
    checks.push({
      check: 'icp_definition',
      pass: false,
      reason: 'ICP configuration not found in fast-io.',
      remediation: 'Define ICP criteria at fast-io key: pitch-config/icp. Required fields: ' + REQUIRED_ICP_FIELDS.join(', ')
    });
  } else {
    const missingFields = REQUIRED_ICP_FIELDS.filter(f => !icpConfig[f] || (Array.isArray(icpConfig[f]) && icpConfig[f].length === 0));
    if (missingFields.length > 0) {
      checks.push({
        check: 'icp_definition',
        pass: false,
        reason: `ICP config missing required fields: ${missingFields.join(', ')}`,
        remediation: `Populate the following fields in fast-io key pitch-config/icp: ${missingFields.join(', ')}`
      });
    } else {
      checks.push({ check: 'icp_definition', pass: true });
    }
  }

  // --- Requirement 2: Bounce Rate ---
  const bounceRate = input?.bounce_rate ?? null;
  if (bounceRate === null) {
    checks.push({
      check: 'bounce_rate',
      pass: false,
      reason: 'No bounce rate data available. Cannot verify data quality.',
      remediation: 'Connect your outbound email platform analytics or run an initial test send to establish baseline bounce rate.'
    });
  } else if (bounceRate > BOUNCE_RATE_THRESHOLD) {
    checks.push({
      check: 'bounce_rate',
      pass: false,
      reason: `Bounce rate ${(bounceRate * 100).toFixed(1)}% exceeds ${(BOUNCE_RATE_THRESHOLD * 100)}% threshold.`,
      remediation: 'Audit contact data quality. Run email verification through Apollo before scaling outreach. Remove unverifiable contacts.'
    });
  } else {
    checks.push({ check: 'bounce_rate', pass: true, bounce_rate: bounceRate });
  }

  // --- Requirement 3: Test Cohort Phase ---
  const launchState = input?.launch_state || null;
  if (!launchState) {
    checks.push({
      check: 'launch_phase',
      pass: false,
      reason: 'No launch state found. Pitch has not completed the test cohort phase.',
      remediation: `Run a test cohort of ${TEST_COHORT_MIN}-${TEST_COHORT_MAX} accounts with full human review before scaling.`
    });
  } else if (launchState.phase === 'test_cohort') {
    if (launchState.accounts_processed >= TEST_COHORT_MIN && launchState.cohort_reviewed) {
      checks.push({
        check: 'launch_phase',
        pass: true,
        note: 'Test cohort complete and reviewed. Ready to transition to scaling phase.'
      });
    } else {
      checks.push({
        check: 'launch_phase',
        pass: false,
        reason: `Test cohort in progress: ${launchState.accounts_processed}/${TEST_COHORT_MIN} accounts processed, reviewed: ${launchState.cohort_reviewed}`,
        remediation: launchState.accounts_processed < TEST_COHORT_MIN
          ? `Process ${TEST_COHORT_MIN - launchState.accounts_processed} more accounts before review.`
          : 'Mark cohort as reviewed to transition to scaling phase.'
      });
    }
  } else if (launchState.phase === 'scaling') {
    checks.push({ check: 'launch_phase', pass: true });
  }

  // --- Requirement 5: CAN-SPAM Config ---
  const canspamConfig = input?.canspam_config || null;
  if (!canspamConfig) {
    checks.push({
      check: 'canspam_config',
      pass: false,
      reason: 'CAN-SPAM configuration not found.',
      remediation: 'Configure physical address and unsubscribe mechanism in your outbound email platform before activating sequences.'
    });
  } else {
    const issues = [];
    if (!canspamConfig.physical_address) issues.push('physical address missing');
    if (!canspamConfig.unsubscribe_mechanism) issues.push('unsubscribe mechanism not configured');
    if (!canspamConfig.opt_out_processing_days || canspamConfig.opt_out_processing_days > 10) {
      issues.push('opt-out processing window exceeds 10 business days or not configured');
    }
    if (issues.length > 0) {
      checks.push({
        check: 'canspam_config',
        pass: false,
        reason: `CAN-SPAM issues: ${issues.join('; ')}`,
        remediation: 'Fix the above issues in your outbound email platform template configuration.'
      });
    } else {
      checks.push({ check: 'canspam_config', pass: true });
    }
  }

  // --- Result ---
  const allPassed = checks.every(c => c.pass);
  const result = {
    pass: allPassed,
    checks,
    timestamp: new Date().toISOString()
  };

  process.stdout.write(JSON.stringify(result));
  process.exit(allPassed ? 0 : 1);
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

main().catch(err => {
  process.stderr.write(err.message);
  process.exit(1);
});
