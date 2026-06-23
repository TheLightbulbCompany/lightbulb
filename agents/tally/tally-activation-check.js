#!/usr/bin/env node
/**
 * tally-activation-check.js
 * Requirements 1, 2, 5: Validate financial connections before operating.
 *
 * Deterministic. Zero LLM.
 */

async function main() {
  const input = await readStdin();
  const checks = [];

  // Requirement 1: Bank/payment connections
  const bankConnection = input?.bank_connection;
  if (!bankConnection || !bankConnection.connected) {
    checks.push({
      check: 'bank_connection',
      pass: false,
      severity: 'blocker',
      reason: 'No bank account connected via Plaid. Tally cannot operate without real-time transaction data.',
      remediation: 'Connect bank accounts and credit cards via Plaid API. Configure with read-only transaction access.'
    });
  } else {
    checks.push({ check: 'bank_connection', pass: true, accounts: bankConnection.account_count || 0 });
  }

  const stripeConnection = input?.stripe_connection;
  if (!stripeConnection || !stripeConnection.connected) {
    checks.push({
      check: 'stripe_connection',
      pass: false,
      severity: 'warning',
      reason: 'Stripe not connected. Revenue tracking and payment matching will be limited.',
      remediation: 'Connect Stripe via stripe-api skill with read-only scope.'
    });
  } else {
    checks.push({ check: 'stripe_connection', pass: true, mode: stripeConnection.scope || 'unknown' });
  }

  // Requirement 2: Accounting software
  const accountingSoftware = input?.accounting_software;
  if (!accountingSoftware || !accountingSoftware.connected) {
    checks.push({
      check: 'accounting_software',
      pass: false,
      severity: 'blocker',
      reason: 'No accounting software connected. Tally needs a ledger of record (Xero, QuickBooks, or equivalent).',
      remediation: 'Connect your accounting software (Xero or QuickBooks) in settings. Tally needs a ledger of record.'
    });
  } else {
    checks.push({ check: 'accounting_software', pass: true, platform: accountingSoftware.platform });
  }

  // Requirement 5: Chart of accounts
  const chartOfAccounts = input?.chart_of_accounts;
  if (!chartOfAccounts || !chartOfAccounts.accounts || chartOfAccounts.accounts.length === 0) {
    checks.push({
      check: 'chart_of_accounts',
      pass: false,
      severity: 'blocker',
      reason: 'Chart of accounts not loaded. Tally needs the chart to categorize transactions correctly.',
      remediation: 'Chart of accounts will be read from connected accounting software. Ensure the accounting software has a configured chart.'
    });
  } else {
    checks.push({ check: 'chart_of_accounts', pass: true, account_count: chartOfAccounts.accounts.length });
  }

  // Approval preferences
  const approvalPrefs = input?.approval_preferences;
  if (!approvalPrefs) {
    checks.push({
      check: 'approval_preferences',
      pass: false,
      severity: 'warning',
      reason: 'Approval preferences not configured. Using defaults: all transactions require individual approval, Slack notifications.',
      remediation: 'Configure auto-confirm vendors, review threshold, and notification channel during setup.'
    });
  } else {
    checks.push({ check: 'approval_preferences', pass: true });
  }

  const blockers = checks.filter(c => !c.pass && c.severity === 'blocker');
  const allPassed = blockers.length === 0;

  const result = {
    pass: allPassed,
    blockers,
    warnings: checks.filter(c => !c.pass && c.severity === 'warning'),
    checks,
    timestamp: new Date().toISOString()
  };

  process.stdout.write(JSON.stringify(result));
  // A failed gate is a branch signal (result.pass=false), not a script error — exit 0 so the
  // workflow can read the JSON and branch via condition: instead of aborting the whole run.
  process.exit(0);
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
