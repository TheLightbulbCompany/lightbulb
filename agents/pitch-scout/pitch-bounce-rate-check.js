#!/usr/bin/env node
/**
 * pitch-bounce-rate-check.js
 * Requirement 2: System-level bounce rate must be below 10%.
 *
 * Reads aggregate bounce data from the connected outbound platform analytics or Apollo stats.
 * Deterministic. Zero LLM.
 */

const BOUNCE_RATE_THRESHOLD = 0.10;
const INDUSTRY_WARNING_THRESHOLD = 0.05;

async function main() {
  const input = await readStdin();
  if (!input) {
    process.stderr.write('No input provided');
    process.exit(1);
  }

  const analytics = input.analytics || {};
  const totalSent = analytics.total_sent || 0;
  const totalBounced = analytics.total_bounced || 0;
  const hardBounces = analytics.hard_bounces || 0;
  const softBounces = analytics.soft_bounces || 0;

  // No data yet — cannot verify
  if (totalSent === 0) {
    const result = {
      pass: false,
      bounce_rate: null,
      reason: 'No send data available. Cannot verify data quality.',
      remediation: 'Run a test send or connect your outbound email platform analytics to establish baseline.',
      timestamp: new Date().toISOString()
    };
    process.stdout.write(JSON.stringify(result));
    process.exit(1);
    return;
  }

  const bounceRate = totalBounced / totalSent;
  const hardBounceRate = hardBounces / totalSent;

  let pass = true;
  let severity = 'ok';
  let remediation = null;

  if (bounceRate > BOUNCE_RATE_THRESHOLD) {
    pass = false;
    severity = 'critical';
    remediation = [
      'Outreach is blocked until bounce rate drops below 10%.',
      'Audit contact list — remove unverifiable emails.',
      'Run remaining contacts through Apollo email verification.',
      `Current bounce rate: ${(bounceRate * 100).toFixed(1)}%. Hard bounces: ${(hardBounceRate * 100).toFixed(1)}%.`,
      'Hard bounces indicate invalid addresses. Soft bounces may resolve on retry.',
      'ISPs typically flag sender domains at sustained rates above 5%.'
    ];
  } else if (bounceRate > INDUSTRY_WARNING_THRESHOLD) {
    severity = 'warning';
    remediation = [
      `Bounce rate ${(bounceRate * 100).toFixed(1)}% is below the 10% hard limit but above the 5% industry safe zone.`,
      'Consider cleaning contact list proactively to prevent domain reputation damage.'
    ];
  }

  const result = {
    pass,
    bounce_rate: Math.round(bounceRate * 1000) / 1000,
    hard_bounce_rate: Math.round(hardBounceRate * 1000) / 1000,
    total_sent: totalSent,
    total_bounced: totalBounced,
    hard_bounces: hardBounces,
    soft_bounces: softBounces,
    threshold: BOUNCE_RATE_THRESHOLD,
    severity,
    remediation,
    timestamp: new Date().toISOString()
  };

  process.stdout.write(JSON.stringify(result));
  process.exit(pass ? 0 : 1);
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
