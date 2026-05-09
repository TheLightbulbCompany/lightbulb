#!/usr/bin/env node
/**
 * pitch-sequence-scheduler.js
 * Requirement 24: C-suite title check for approval routing
 * Requirement 28: Schedule follow-up touches 2-5
 * Requirement 30: 5-touch maximum enforcement (redundant with compliance check)
 * Requirement 31: Timing optimization based on engagement patterns
 *
 * Deterministic scheduling. Zero LLM.
 */

const { isCsuite } = require('./pitch-compliance-check.js');

const MAX_TOUCHES = 5;

// Default timing rules (overridden by learned weights from fast-io)
const DEFAULT_TIMING = {
  email: {
    preferred_days: [2, 3, 4], // Tue, Wed, Thu (0=Sun)
    preferred_hours: [9, 10, 11], // 9-11 AM
    min_gap_hours: 48,
    max_gap_hours: 120
  },
  linkedin: {
    preferred_days: [2, 3],
    preferred_hours: [7, 8, 9],
    min_gap_hours: 72,
    max_gap_hours: 168
  },
  sms: {
    preferred_days: [1, 2, 3, 4, 5],
    preferred_hours: [10, 11, 14, 15],
    min_gap_hours: 48,
    max_gap_hours: 96
  },
  phone: {
    preferred_days: [2, 3, 4],
    preferred_hours: [10, 11, 14, 15, 16],
    min_gap_hours: 72,
    max_gap_hours: 168
  }
};

function calculateOptimalSendTime(prospect, channel, timingWeights) {
  const timing = timingWeights?.[channel] || DEFAULT_TIMING[channel] || DEFAULT_TIMING.email;
  const now = new Date();

  // If prospect has engagement history, use their patterns
  if (prospect.engagement_history && prospect.engagement_history.length > 0) {
    const engagements = prospect.engagement_history
      .filter(e => e.type === 'open' || e.type === 'click' || e.type === 'reply')
      .map(e => new Date(e.timestamp));

    if (engagements.length >= 3) {
      // Find most common hour of engagement
      const hourCounts = {};
      engagements.forEach(e => {
        const h = e.getHours();
        hourCounts[h] = (hourCounts[h] || 0) + 1;
      });
      const bestHour = Object.entries(hourCounts)
        .sort((a, b) => b[1] - a[1])[0][0];

      // Find most common day
      const dayCounts = {};
      engagements.forEach(e => {
        const d = e.getDay();
        dayCounts[d] = (dayCounts[d] || 0) + 1;
      });
      const bestDay = Object.entries(dayCounts)
        .sort((a, b) => b[1] - a[1])[0][0];

      return {
        preferred_hour: parseInt(bestHour),
        preferred_day: parseInt(bestDay),
        source: 'prospect_engagement_history'
      };
    }
  }

  // Fall back to default timing rules
  return {
    preferred_hour: timing.preferred_hours[0],
    preferred_day: timing.preferred_days[0],
    source: 'default_timing_rules'
  };
}

function getNextSendTime(lastTouchDate, channel, prospect, timingWeights) {
  const timing = timingWeights?.[channel] || DEFAULT_TIMING[channel] || DEFAULT_TIMING.email;
  const lastTouch = new Date(lastTouchDate);
  const minNextTime = new Date(lastTouch.getTime() + timing.min_gap_hours * 60 * 60 * 1000);
  const optimal = calculateOptimalSendTime(prospect, channel, timingWeights);

  // Find the next occurrence of the preferred day/hour after the minimum gap
  let candidate = new Date(minNextTime);
  for (let i = 0; i < 14; i++) { // look ahead up to 2 weeks
    candidate.setDate(minNextTime.getDate() + i);
    if (timing.preferred_days.includes(candidate.getDay())) {
      candidate.setHours(optimal.preferred_hour, 0, 0, 0);
      if (candidate > minNextTime) {
        return {
          send_at: candidate.toISOString(),
          timing_source: optimal.source
        };
      }
    }
  }

  // Fallback: just use minimum gap
  return {
    send_at: minNextTime.toISOString(),
    timing_source: 'minimum_gap_fallback'
  };
}

async function main() {
  const input = await readStdin();
  if (!input) {
    process.stderr.write('No input provided');
    process.exit(1);
  }

  const sequences = input.sequences || [];
  const timingWeights = input.timing_weights || {};
  const now = new Date();

  const dueTouches = [];
  const skipped = [];

  for (const seq of sequences) {
    // Requirement 30: 5-touch max (belt and suspenders with compliance check)
    if (seq.touch_count >= MAX_TOUCHES) {
      skipped.push({
        domain: seq.prospect_domain,
        reason: 'max_touches_reached',
        touch_count: seq.touch_count
      });
      continue;
    }

    // Check if paused
    if (seq.paused) {
      skipped.push({
        domain: seq.prospect_domain,
        reason: 'sequence_paused',
        pause_reason: seq.pause_reason
      });
      continue;
    }

    // Calculate next send time
    const nextTouch = getNextSendTime(
      seq.last_touch_date,
      seq.channel,
      seq.prospect,
      timingWeights
    );

    const sendAt = new Date(nextTouch.send_at);

    // Is it due now?
    if (sendAt <= now) {
      const touchData = {
        domain: seq.prospect_domain,
        touch_number: seq.touch_count + 1,
        channel: seq.channel,
        prospect: seq.prospect,
        sequence_id: seq.sequence_id,
        send_at: nextTouch.send_at,
        timing_source: nextTouch.timing_source,
        // Requirement 24: C-suite check
        requires_approval: isCsuite(seq.prospect?.title),
        approval_reason: isCsuite(seq.prospect?.title)
          ? 'C-suite contact — follow-up requires rep approval regardless of sequence status'
          : null
      };
      dueTouches.push(touchData);
    }
  }

  const result = {
    due_touches: dueTouches,
    skipped,
    has_due_touches: dueTouches.length > 0,
    requires_approval_count: dueTouches.filter(t => t.requires_approval).length,
    autonomous_count: dueTouches.filter(t => !t.requires_approval).length,
    timestamp: now.toISOString()
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
