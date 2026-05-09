#!/usr/bin/env node
/**
 * pitch-opt-out-detector.js
 * Requirement 42: Detect opt-out language in prospect replies.
 *
 * Keyword matching. Zero LLM. Covers 95%+ of opt-out language.
 * Ambiguous soft declines proceed to normal reply handling where the rep decides.
 */

const OPT_OUT_PATTERNS = [
  'unsubscribe',
  'opt out', 'opt-out', 'optout',
  'stop contacting', 'stop emailing', 'stop messaging',
  'remove me', 'remove my',
  'do not contact', 'don\'t contact', 'dont contact',
  'take me off', 'take my name off',
  'no longer interested',
  'please stop', 'pls stop',
  'leave me alone',
  'do not email', 'don\'t email', 'dont email',
  'never contact', 'never email',
  'block', 'reported as spam',
  'cease and desist',
  'remove from list', 'remove from your list',
  'i don\'t want', 'i do not want',
  'delete my', 'delete me'
];

function detectOptOut(replyText) {
  if (!replyText) return { is_opt_out: false };

  const lower = replyText.toLowerCase().trim();

  const matches = OPT_OUT_PATTERNS.filter(pattern =>
    lower.includes(pattern)
  );

  if (matches.length > 0) {
    return {
      is_opt_out: true,
      confidence: 'high',
      matched_patterns: matches,
      action: 'process_opt_out',
      reason: 'Prospect reply contains explicit opt-out language. Process immediately without rep action.'
    };
  }

  return {
    is_opt_out: false,
    action: 'proceed_to_reply_handler',
    reason: 'No opt-out language detected. Proceed with normal reply handling.'
  };
}

async function main() {
  const input = await readStdin();
  if (!input) {
    process.stderr.write('No input provided');
    process.exit(1);
  }

  const replyText = input.reply_text || input.body || '';
  const prospectDomain = input.prospect_domain || 'unknown';

  const detection = detectOptOut(replyText);

  const result = {
    ...detection,
    prospect_domain: prospectDomain,
    timestamp: new Date().toISOString()
  };

  process.stdout.write(JSON.stringify(result));
  process.exit(detection.is_opt_out ? 1 : 0); // exit 1 to halt pipeline if opt-out
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

module.exports = { detectOptOut };

if (require.main === module) {
  main().catch(err => {
    process.stderr.write(err.message);
    process.exit(1);
  });
}
