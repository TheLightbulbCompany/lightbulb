#!/usr/bin/env node
/**
 * thread-injection-detector.js
 * Requirement 9: Flag messages with injection-style language.
 *
 * Deterministic pattern matching (~90%). Adaptive dismissal suppression.
 */

const INJECTION_PATTERNS = [
  // Direct injection attempts
  { pattern: /ignore\s+(all\s+)?previous\s+instructions/i, severity: 'critical', type: 'direct_override' },
  { pattern: /you\s+are\s+now\s+(in\s+)?/i, severity: 'critical', type: 'identity_override' },
  { pattern: /developer\s+mode/i, severity: 'critical', type: 'mode_switch' },
  { pattern: /system\s+prompt/i, severity: 'high', type: 'prompt_extraction' },
  { pattern: /jailbreak/i, severity: 'high', type: 'jailbreak' },
  { pattern: /act\s+as\s+(an?\s+)?AI/i, severity: 'high', type: 'role_assignment' },
  { pattern: /disregard\s+(your|all)\s+(rules|instructions|guidelines)/i, severity: 'critical', type: 'direct_override' },

  // Indirect injection (command language addressed to AI)
  { pattern: /please\s+send\s+(an?\s+)?email\s+to/i, severity: 'medium', type: 'indirect_command' },
  { pattern: /forward\s+this\s+(message|email)\s+to/i, severity: 'medium', type: 'indirect_command' },
  { pattern: /reply\s+to\s+all\s+(with|saying)/i, severity: 'medium', type: 'indirect_command' },
  { pattern: /share\s+(my|this|the)\s+(api|key|password|token|credential)/i, severity: 'critical', type: 'exfiltration' },
  { pattern: /send\s+(all|my)\s+(contacts|emails|messages)\s+to/i, severity: 'critical', type: 'exfiltration' },

  // Encoded/obfuscated
  { pattern: /base64[:\s]/i, severity: 'medium', type: 'encoding' },
  { pattern: /\[hidden\]/i, severity: 'medium', type: 'hidden_content' },
  { pattern: /<!--[\s\S]*?-->/g, severity: 'medium', type: 'html_comment' }
];

function detectInjection(text, senderEmail, safeSenders) {
  const lower = (text || '').toLowerCase();
  const detections = [];

  for (const { pattern, severity, type } of INJECTION_PATTERNS) {
    if (pattern.test(text)) {
      detections.push({ pattern: pattern.source, severity, type });
    }
  }

  if (detections.length === 0) {
    return { flagged: false, detections: [] };
  }

  // Check if sender is in known-safe list (dismissal suppression)
  const senderKey = (senderEmail || '').toLowerCase();
  const isSafeSender = (safeSenders || []).includes(senderKey);
  const maxSeverity = detections.reduce((max, d) =>
    d.severity === 'critical' ? 'critical' :
    d.severity === 'high' && max !== 'critical' ? 'high' :
    d.severity === 'medium' && max === 'medium' ? 'medium' : max, 'medium');

  // Safe senders still flagged for critical patterns, suppressed for medium
  const effectiveFlag = isSafeSender && maxSeverity === 'medium' ? false : true;

  return {
    flagged: effectiveFlag,
    suppressed: isSafeSender && !effectiveFlag,
    detections,
    max_severity: maxSeverity,
    sender: senderEmail,
    is_safe_sender: isSafeSender,
    message: effectiveFlag
      ? `⚠️ This message may contain content designed to influence AI behavior (${detections.length} pattern(s) detected: ${detections.map(d => d.type).join(', ')}). Review before processing.`
      : null,
    timestamp: new Date().toISOString()
  };
}

async function main() {
  const input = await readStdin();
  if (!input) { process.stderr.write('No input'); process.exit(1); }

  const result = detectInjection(
    input.text || input.sanitized_text || '',
    input.sender_email || input.sender || '',
    input.safe_senders || []
  );

  process.stdout.write(JSON.stringify(result));
  // "flagged" is a branch decision carried in JSON (message-inbound gates on
  // $injection-check.json.flagged); detecting injection is not a script error.
  process.exit(0);
}

function readStdin() {
  return new Promise((resolve) => {
    let data = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', chunk => data += chunk);
    process.stdin.on('end', () => { try { resolve(JSON.parse(data)); } catch { resolve(null); } });
    if (process.stdin.isTTY) resolve(null);
  });
}

module.exports = { detectInjection };

if (require.main === module) { main().catch(err => { process.stderr.write(err.message); process.exit(1); }); }
