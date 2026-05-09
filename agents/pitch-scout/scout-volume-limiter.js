#!/usr/bin/env node
/**
 * scout-volume-limiter.js
 * Requirements 54, 64: Configurable daily volume limit, default 50.
 * Excess leads buffered for next day in score order.
 *
 * Deterministic counter. Zero LLM.
 */

const DEFAULT_DAILY_LIMIT = 50;

async function main() {
  const input = await readStdin();
  if (!input) {
    process.stderr.write('No input provided');
    process.exit(1);
  }

  const leads = input.leads || [];
  const todayDeposits = input.today_deposits || 0;
  const dailyLimit = input.daily_limit || DEFAULT_DAILY_LIMIT;

  const remaining = Math.max(0, dailyLimit - todayDeposits);

  // Sort by score descending — highest scores get deposited first
  const sorted = [...leads].sort((a, b) => (b.score || 0) - (a.score || 0));

  const toDeposit = sorted.slice(0, remaining);
  const toBuffer = sorted.slice(remaining);

  const result = {
    deposit: toDeposit,
    buffer: toBuffer,
    deposited_count: toDeposit.length,
    buffered_count: toBuffer.length,
    today_total: todayDeposits + toDeposit.length,
    daily_limit: dailyLimit,
    at_limit: todayDeposits + toDeposit.length >= dailyLimit,
    timestamp: new Date().toISOString()
  };

  process.stdout.write(JSON.stringify(result));
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
