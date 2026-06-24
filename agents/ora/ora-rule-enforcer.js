#!/usr/bin/env node
/**
 * ora-rule-enforcer.js
 * Requirements 7, 9, 50: Enforce scheduling rules with adaptive exception handling.
 *
 * Deterministic rule checks with agent loop escape hatch for learned exceptions.
 */

function enforceRules(request, rules, todayMeetings, exceptionHistory) {
  const violations = [];
  const requestStart = new Date(request.start);
  const requestEnd = new Date(request.end);
  const requestHourStart = requestStart.getHours() + requestStart.getMinutes() / 60;
  const requestHourEnd = requestEnd.getHours() + requestEnd.getMinutes() / 60;
  const requestDay = requestStart.getDay(); // 0=Sun, 6=Sat

  // Parse rule times
  const earliestHour = parseTimeToHour(rules.earliest_start || '09:00');
  const latestHour = parseTimeToHour(rules.latest_end || '18:00');
  const bufferMinutes = rules.buffer_minutes || 15;
  const maxMeetings = rules.max_meetings_per_day || 8;
  const noMeetingDays = rules.no_meeting_days || [];
  const focusBlocks = todayMeetings.filter(m => m.is_focus_block);
  const neverOverride = rules.never_override || [];

  // Check 1: Before earliest start
  if (requestHourStart < earliestHour) {
    violations.push({
      rule: 'earliest_start',
      detail: `Meeting starts at ${formatTime(requestHourStart)} — your earliest start is ${rules.earliest_start}.`,
      severity: 'reject'
    });
  }

  // Check 2: After latest end
  if (requestHourEnd > latestHour) {
    violations.push({
      rule: 'latest_end',
      detail: `Meeting ends at ${formatTime(requestHourEnd)} — your latest end is ${rules.latest_end}.`,
      severity: 'reject'
    });
  }

  // Check 3: No-meeting day
  const dayNames = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
  if (noMeetingDays.includes(dayNames[requestDay]) || noMeetingDays.includes(requestDay)) {
    violations.push({
      rule: 'no_meeting_day',
      detail: `${dayNames[requestDay]} is a no-meeting day.`,
      severity: 'reject'
    });
  }

  // Check 4: Daily meeting limit
  const todayCount = todayMeetings.filter(m => !m.is_focus_block).length;
  if (todayCount >= maxMeetings) {
    violations.push({
      rule: 'daily_limit',
      detail: `You already have ${todayCount} meetings today — your limit is ${maxMeetings}.`,
      severity: 'reject'
    });
  }

  // Check 5: Focus block overlap (Req 7)
  for (const block of focusBlocks) {
    const blockStart = new Date(block.start);
    const blockEnd = new Date(block.end);
    if (requestStart < blockEnd && requestEnd > blockStart) {
      violations.push({
        rule: 'focus_block',
        detail: `Overlaps with your focus block (${formatTime(blockStart.getHours())} - ${formatTime(blockEnd.getHours())}).`,
        severity: 'reject',
        focus_block_id: block.id
      });
    }
  }

  // Check 6: Buffer violation
  for (const meeting of todayMeetings) {
    if (meeting.is_focus_block) continue;
    const meetEnd = new Date(meeting.end);
    const meetStart = new Date(meeting.start);
    const gapBefore = (requestStart - meetEnd) / (1000 * 60);
    const gapAfter = (meetStart - requestEnd) / (1000 * 60);

    if (gapBefore >= 0 && gapBefore < bufferMinutes) {
      violations.push({
        rule: 'buffer',
        detail: `Only ${Math.round(gapBefore)} minutes after "${meeting.title}" — your buffer is ${bufferMinutes} minutes.`,
        severity: 'reject'
      });
    }
    if (gapAfter >= 0 && gapAfter < bufferMinutes) {
      violations.push({
        rule: 'buffer',
        detail: `Only ${Math.round(gapAfter)} minutes before "${meeting.title}" — your buffer is ${bufferMinutes} minutes.`,
        severity: 'reject'
      });
    }
  }

  // Check 7: Never-override commitments
  for (const commitment of neverOverride) {
    if (request.start === commitment.start || request.title === commitment.title) {
      violations.push({
        rule: 'never_override',
        detail: `Conflicts with "${commitment.description}" which is marked as never-override.`,
        severity: 'hard_reject'
      });
    }
  }

  // === ESCAPE HATCH: Exception pattern detection ===
  // If the user has historically overridden this rule for this person or meeting type,
  // surface the pattern instead of rigid rejection
  const exceptions = exceptionHistory || {};
  const requestPerson = (request.organizer || request.attendee_email || '').toLowerCase();
  const requestType = (request.meeting_type || '').toLowerCase();

  let hasExceptionPattern = false;
  let exceptionContext = null;

  for (const violation of violations) {
    if (violation.severity === 'hard_reject') continue; // never-override stays rigid

    const exKey = `${violation.rule}:${requestPerson}`;
    const typeKey = `${violation.rule}:${requestType}`;

    if ((exceptions[exKey] || 0) >= 2 || (exceptions[typeKey] || 0) >= 2) {
      hasExceptionPattern = true;
      exceptionContext = {
        rule: violation.rule,
        person: requestPerson || requestType,
        override_count: exceptions[exKey] || exceptions[typeKey],
        message: `This violates your ${violation.rule} rule, but you've overridden for ${requestPerson || requestType} ${exceptions[exKey] || exceptions[typeKey]} times before. Override or enforce?`
      };
      break;
    }
  }

  const hasViolations = violations.length > 0;
  const hasHardReject = violations.some(v => v.severity === 'hard_reject');

  return {
    pass: !hasViolations,
    violations,
    action: hasHardReject ? 'hard_reject' :
            hasExceptionPattern ? 'surface_exception' :
            hasViolations ? 'reject_and_suggest' :
            'proceed',
    needs_agent_loop: hasExceptionPattern,
    exception_context: exceptionContext,
    request_summary: {
      title: request.title,
      start: request.start,
      end: request.end,
      organizer: request.organizer
    },
    timestamp: new Date().toISOString()
  };
}

function parseTimeToHour(timeStr) {
  const [h, m] = (timeStr || '09:00').split(':').map(Number);
  return h + (m || 0) / 60;
}

function formatTime(hour) {
  const h = Math.floor(hour);
  const m = Math.round((hour - h) * 60);
  const period = h >= 12 ? 'PM' : 'AM';
  const displayH = h > 12 ? h - 12 : h === 0 ? 12 : h;
  return `${displayH}:${m.toString().padStart(2, '0')} ${period}`;
}

async function main() {
  const input = await readStdin();
  if (!input) {
    process.stderr.write('No input provided');
    process.exit(1);
  }

  // Array mode: when the pipeline passes {slots: [...]} instead of
  // {request: {...}}, run enforceRules on each slot and return
  // {passing_slots, rejected_slots} so the downstream ranker gets
  // the filtered list.
  if (Array.isArray(input.slots)) {
    const rules = input.rules || {};
    const todayMeetings = input.today_meetings || [];
    const exceptionHistory = input.exception_history || {};
    const passing = [];
    const rejected = [];
    let anyNeedsAgentLoop = false;
    let firstException = null;
    for (const slot of input.slots) {
      const r = enforceRules(slot, rules, todayMeetings, exceptionHistory);
      if (r.pass || r.action === 'surface_exception') {
        passing.push(slot);
        if (r.action === 'surface_exception' && !firstException) {
          firstException = r.exception_context;
          anyNeedsAgentLoop = true;
        }
      } else {
        rejected.push({ slot, violations: r.violations, action: r.action });
      }
    }
    const result = {
      passing_slots: passing,
      rejected_slots: rejected,
      passing_count: passing.length,
      rejected_count: rejected.length,
      needs_agent_loop: anyNeedsAgentLoop,
      exception_context: firstException,
    };
    process.stdout.write(JSON.stringify(result));
    process.exit(0);
    return;
  }

  // Single-request mode (original behavior)
  const result = enforceRules(
    input.request || {},
    input.rules || {},
    input.today_meetings || [],
    input.exception_history || {}
  );

  process.stdout.write(JSON.stringify(result));
  // A non-compliant slot is a normal verdict, not a crash — the decision is
  // carried in pass/violations/action; exit 0 so the workflow gates on the JSON.
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

module.exports = { enforceRules };

if (require.main === module) {
  main().catch(err => {
    process.stderr.write(err.message);
    process.exit(1);
  });
}
