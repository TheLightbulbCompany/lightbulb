#!/usr/bin/env node
/**
 * tally-anomaly-detector.js
 * Requirements 21, 22, 24, 25: Anomaly detection, vendor patterns, receipt validation.
 *
 * Deterministic pattern matching. Zero LLM.
 */

function detectAnomalies(transaction, vendorHistory, chartOfAccounts, dismissalHistory) {
  const flags = [];
  const vendor = (transaction.vendor || '').toLowerCase();
  const amount = transaction.amount || 0;
  const history = vendorHistory[vendor];

  // Dismissal history: when the finance person dismisses the same flag type
  // for the same vendor 3+ times, suppress that flag type for that vendor.
  // This prevents the same false positive from surfacing repeatedly.
  const dismissed = dismissalHistory || {};
  const vendorDismissals = dismissed[vendor] || {};
  function isDismissed(flagType) {
    return (vendorDismissals[flagType] || 0) >= 3;
  }

  // Signal 1: New payee
  if (!history && !chartOfAccounts.some(a => a.vendors?.includes(vendor))) {
    flags.push({
      type: 'new_payee',
      severity: 'info',
      detail: `First transaction from "${transaction.vendor}". This vendor is not in your chart of accounts or transaction history.`
    });
  }

  // Signal 2: Amount deviation from pattern (Req 21)
  if (history && history.amounts && history.amounts.length >= 3) {
    const median = getMedian(history.amounts);
    const deviation = Math.abs(amount - median) / median;
    if (deviation > 0.15) {
      flags.push({
        type: 'amount_deviation',
        severity: deviation > 0.5 ? 'warning' : 'info',
        detail: `$${amount} is ${Math.round(deviation * 100)}% ${amount > median ? 'higher' : 'lower'} than the median $${median.toFixed(2)} from this vendor's last ${history.amounts.length} charges.`
      });
    }
  }

  // Signal 3: Unusual timing
  if (transaction.date) {
    const day = new Date(transaction.date).getDay();
    if (history && history.typical_days && history.typical_days.length > 0) {
      if (!history.typical_days.includes(day)) {
        const dayNames = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
        flags.push({
          type: 'unusual_timing',
          severity: 'info',
          detail: `Charged on ${dayNames[day]}. This vendor typically charges on ${history.typical_days.map(d => dayNames[d]).join(', ')}.`
        });
      }
    }
    // Weekend charge from business vendor
    if ((day === 0 || day === 6) && history && !history.weekend_charges) {
      flags.push({
        type: 'weekend_charge',
        severity: 'info',
        detail: `Weekend charge from a vendor that has never billed on a weekend before.`
      });
    }
  }

  // Signal 4: Round number from irregular vendor
  if (history && history.amounts && history.amounts.length >= 3) {
    const hasIrregularHistory = history.amounts.some(a => a % 1 !== 0);
    const isRound = amount % 100 === 0 || amount % 50 === 0;
    if (hasIrregularHistory && isRound && amount > 100) {
      flags.push({
        type: 'round_number',
        severity: 'info',
        detail: `Round amount ($${amount}) from a vendor that normally invoices irregular amounts (e.g., $${history.amounts[0]?.toFixed(2)}).`
      });
    }
  }

  // Signal 5: Off-cycle billing
  if (history && history.billing_cycle_days && history.billing_cycle_days > 0 && history.last_charge_date) {
    const daysSinceLast = (new Date(transaction.date) - new Date(history.last_charge_date)) / (1000 * 60 * 60 * 24);
    const expectedCycle = history.billing_cycle_days;
    const cycleDeviation = Math.abs(daysSinceLast - expectedCycle) / expectedCycle;
    if (cycleDeviation > 0.3 && daysSinceLast < expectedCycle * 0.7) {
      flags.push({
        type: 'off_cycle',
        severity: 'warning',
        detail: `Billed ${Math.round(daysSinceLast)} days after last charge. Expected billing cycle is ~${expectedCycle} days. This charge arrived ${Math.round(expectedCycle - daysSinceLast)} days early.`
      });
    }
  }

  // Filter out flags the finance person has repeatedly dismissed for this vendor
  const activeFlags = flags.filter(f => !isDismissed(f.type));
  const suppressedFlags = flags.filter(f => isDismissed(f.type));

  // Escape hatch: if no flags matched but the transaction feels unusual
  // (very high amount, very unusual vendor name, or transaction metadata suggests
  // something the 6 signal types don't cover), flag for agent loop review
  const isVeryHighAmount = amount > (history?.amount_max || Infinity) * 3;
  const isUnusualMetadata = transaction.metadata?.needs_review;
  if (activeFlags.length === 0 && (isVeryHighAmount || isUnusualMetadata)) {
    activeFlags.push({
      type: 'recommend_agent_review',
      severity: 'advisory',
      detail: 'Transaction passed all standard checks but has unusual characteristics. Agent loop should review for patterns the standard signals may miss.',
      needs_agent_loop: true
    });
  }

  return { flags: activeFlags, suppressed: suppressedFlags };
}

function validateReceipt(receipt, transaction) {
  const flags = [];

  if (!receipt) return flags;

  // Amount mismatch
  if (receipt.total && Math.abs(receipt.total - transaction.amount) > 0.01) {
    flags.push({
      type: 'receipt_amount_mismatch',
      severity: 'warning',
      detail: `Receipt total $${receipt.total} does not match bank charge $${transaction.amount}. Investigate before approving.`
    });
  }

  // Vendor mismatch
  if (receipt.vendor && transaction.vendor) {
    const receiptVendor = receipt.vendor.toLowerCase().replace(/[^a-z0-9]/g, '');
    const txVendor = transaction.vendor.toLowerCase().replace(/[^a-z0-9]/g, '');
    if (!receiptVendor.includes(txVendor) && !txVendor.includes(receiptVendor)) {
      flags.push({
        type: 'receipt_vendor_mismatch',
        severity: 'warning',
        detail: `Receipt vendor "${receipt.vendor}" does not match transaction vendor "${transaction.vendor}".`
      });
    }
  }

  // Date mismatch
  if (receipt.date && transaction.date) {
    const daysDiff = Math.abs(new Date(receipt.date) - new Date(transaction.date)) / (1000 * 60 * 60 * 24);
    if (daysDiff > 7) {
      flags.push({
        type: 'receipt_date_mismatch',
        severity: 'info',
        detail: `Receipt date ${receipt.date} is ${Math.round(daysDiff)} days from transaction date ${transaction.date}.`
      });
    }
  }

  // Tax math check
  if (receipt.subtotal && receipt.tax && receipt.total) {
    const expectedTotal = receipt.subtotal + receipt.tax;
    if (Math.abs(expectedTotal - receipt.total) > 0.02) {
      flags.push({
        type: 'receipt_math_error',
        severity: 'warning',
        detail: `Receipt math doesn't add up: subtotal $${receipt.subtotal} + tax $${receipt.tax} = $${expectedTotal.toFixed(2)}, but total shows $${receipt.total}.`
      });
    }
  }

  return flags;
}

function getMedian(arr) {
  const sorted = [...arr].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

async function main() {
  const input = await readStdin();
  if (!input) {
    process.stderr.write('No input provided');
    process.exit(1);
  }

  const transactions = input.transactions || [input.transaction || input];
  const vendorHistory = input.vendor_history || {};
  const chartOfAccounts = input.chart_of_accounts || [];
  const dismissalHistory = input.dismissal_history || {};

  const results = transactions.map(tx => {
    const anomalyResult = detectAnomalies(tx, vendorHistory, chartOfAccounts, dismissalHistory);
    const receiptFlags = tx.receipt ? validateReceipt(tx.receipt, tx) : [];
    const allFlags = [...(anomalyResult.flags || []), ...receiptFlags];

    return {
      transaction_id: tx.id || tx.transaction_id,
      vendor: tx.vendor,
      amount: tx.amount,
      date: tx.date,
      anomaly_flags: allFlags,
      has_flags: allFlags.length > 0,
      highest_severity: allFlags.reduce((max, f) =>
        f.severity === 'warning' ? 'warning' : max, allFlags.length > 0 ? 'info' : 'none')
    };
  });

  const output = {
    results,
    flagged_count: results.filter(r => r.has_flags).length,
    warning_count: results.filter(r => r.highest_severity === 'warning').length,
    clean_count: results.filter(r => !r.has_flags).length,
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

module.exports = { detectAnomalies, validateReceipt };

if (require.main === module) {
  main().catch(err => {
    process.stderr.write(err.message);
    process.exit(1);
  });
}
