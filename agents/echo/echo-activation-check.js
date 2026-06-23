#!/usr/bin/env node
/**
 * echo-activation-check.js
 * Requirements 1-4: Validate transcription, templates, reviewer, consent.
 */

async function main() {
  const input = await readStdin();
  const checks = [];

  // Meeting platform (click-to-connect). Warning, not blocker — pipelines no-op
  // gracefully until the user connects a platform in settings.
  const meetingPlatforms = input?.meeting_platforms || {};
  const anyConnected = ['zoom', 'gmeet', 'teams'].some(p => meetingPlatforms[p]?.connected);
  if (!anyConnected) {
    checks.push({
      check: 'meeting_platform',
      pass: false,
      severity: 'warning',
      reason: 'No meeting platform connected. Echo cannot pull transcripts until a platform is connected in settings.',
      remediation: 'Connect Zoom, Google Meet, or Microsoft Teams in the Isol8 settings UI. Multiple can be connected simultaneously.'
    });
  } else {
    checks.push({
      check: 'meeting_platform',
      pass: true,
      connected: Object.keys(meetingPlatforms).filter(p => meetingPlatforms[p]?.connected)
    });
  }

  // Meeting type templates
  const templates = input?.templates || {};
  if (Object.keys(templates).length === 0) {
    checks.push({
      check: 'templates',
      pass: false,
      severity: 'blocker',
      reason: 'No meeting type templates configured. Echo needs at least one template to know what level of documentation each meeting requires.',
      remediation: 'Configure templates: board, standup, sales_call, design_review, one_on_one.'
    });
  } else {
    // Check each template has a reviewer
    const missingReviewers = Object.entries(templates).filter(([_, t]) => !t.reviewer);
    if (missingReviewers.length > 0) {
      checks.push({
        check: 'reviewers',
        pass: false,
        severity: 'blocker',
        reason: `Templates without designated reviewer: ${missingReviewers.map(([k]) => k).join(', ')}. The review gate requires a named person.`,
        remediation: 'Assign a reviewer for each meeting type template.'
      });
    } else {
      checks.push({ check: 'templates', pass: true, count: Object.keys(templates).length });
    }
  }

  // Recording consent
  const consent = input?.consent;
  if (!consent || !consent.confirmed) {
    checks.push({
      check: 'consent',
      pass: false,
      severity: 'blocker',
      reason: 'Recording consent not configured. Echo cannot join or process any meeting without confirmed consent practices. This is a legal requirement in multiple jurisdictions.',
      remediation: 'Confirm consent practices during setup.'
    });
  } else {
    checks.push({ check: 'consent', pass: true, method: consent.method });
  }

  // Commitment language thresholds
  const thresholds = input?.commitment_thresholds;
  if (!thresholds) {
    checks.push({
      check: 'commitment_thresholds',
      pass: true,
      note: 'Using defaults: "I will/I\'ll/we\'re going with" = definitive. "We should/might/let\'s think about" = tentative.'
    });
  } else {
    checks.push({ check: 'commitment_thresholds', pass: true, custom: true });
  }

  // Calendar connection (for attendee lists and meeting type detection)
  const calendar = input?.calendar;
  if (!calendar || !calendar.connected) {
    checks.push({
      check: 'calendar',
      pass: false,
      severity: 'warning',
      reason: 'Google Calendar not connected. Echo won\'t be able to auto-detect attendees or meeting types.',
      remediation: 'Connect via gog for automatic attendee and meeting type detection.'
    });
  } else {
    checks.push({ check: 'calendar', pass: true });
  }

  const blockers = checks.filter(c => !c.pass && c.severity === 'blocker');
  const result = {
    pass: blockers.length === 0,
    blockers,
    warnings: checks.filter(c => !c.pass && c.severity === 'warning'),
    checks,
    timestamp: new Date().toISOString()
  };

  process.stdout.write(JSON.stringify(result));
  // Exit 0 even with blockers — the pass/blockers decision is in stdout JSON and
  // the workflow gates on it; a nonzero exit would abort the run under on_error:stop.
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

if (require.main === module) { main().catch(err => { process.stderr.write(err.message); process.exit(1); }); }
