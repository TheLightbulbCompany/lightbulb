#!/usr/bin/env node
/**
 * pitch-sequence-pauser.js
 * Requirement 29/33: Pause active sequences when a trigger condition fires.
 *
 * Updates sequence state in fast-io. Deterministic. Zero LLM.
 */

async function main() {
  const input = await readStdin();
  if (!input) {
    process.stderr.write('No input provided');
    process.exit(1);
  }

  const domain = input.prospect_domain || input.domain;
  const reason = input.reason || 'manual_pause';
  const sequenceId = input.sequence_id || null;

  if (!domain) {
    process.stderr.write('prospect_domain is required');
    process.exit(1);
  }

  const result = {
    action: 'pause_sequence',
    prospect_domain: domain,
    sequence_id: sequenceId,
    paused: true,
    pause_reason: reason,
    pause_timestamp: new Date().toISOString(),
    fast_io_key: `sequences/active/${domain}`,
    update: {
      paused: true,
      pause_reason: reason,
      pause_timestamp: new Date().toISOString()
    }
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
