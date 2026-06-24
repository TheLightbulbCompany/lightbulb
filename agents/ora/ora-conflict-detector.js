#!/usr/bin/env node
/**
 * ora-conflict-detector.js
 * Requirement 10: Cross-calendar conflict detection in real time.
 *
 * Deterministic time-range comparison. Zero LLM.
 */

function detectConflicts(events) {
  const conflicts = [];

  // Sort by start time
  const sorted = [...events].sort((a, b) => new Date(a.start) - new Date(b.start));

  for (let i = 0; i < sorted.length; i++) {
    for (let j = i + 1; j < sorted.length; j++) {
      const a = sorted[i];
      const b = sorted[j];

      const aStart = new Date(a.start);
      const aEnd = new Date(a.end);
      const bStart = new Date(b.start);
      const bEnd = new Date(b.end);

      // No overlap possible if b starts after a ends
      if (bStart >= aEnd) break;

      // Overlap detected
      if (aStart < bEnd && aEnd > bStart) {
        // Skip if same calendar and same event (duplicate from sync)
        if (a.calendar_id === b.calendar_id && a.event_id === b.event_id) continue;

        const overlapStart = new Date(Math.max(aStart, bStart));
        const overlapEnd = new Date(Math.min(aEnd, bEnd));
        const overlapMinutes = (overlapEnd - overlapStart) / (1000 * 60);

        conflicts.push({
          event_a: {
            title: a.title,
            calendar: a.calendar_source || a.calendar_id,
            start: a.start,
            end: a.end,
            is_focus_block: a.is_focus_block || false
          },
          event_b: {
            title: b.title,
            calendar: b.calendar_source || b.calendar_id,
            start: b.start,
            end: b.end,
            is_focus_block: b.is_focus_block || false
          },
          overlap_minutes: Math.round(overlapMinutes),
          cross_calendar: a.calendar_source !== b.calendar_source,
          severity: a.is_focus_block || b.is_focus_block ? 'focus_block_conflict' :
                    overlapMinutes >= 30 ? 'full_overlap' : 'partial_overlap'
        });
      }
    }
  }

  return conflicts;
}

async function main() {
  const input = await readStdin();
  if (!input) {
    process.stderr.write('No input provided');
    process.exit(1);
  }

  const events = input.events || [];
  const conflicts = detectConflicts(events);

  const result = {
    has_conflicts: conflicts.length > 0,
    conflicts,
    total_events_checked: events.length,
    cross_calendar_conflicts: conflicts.filter(c => c.cross_calendar).length,
    focus_block_conflicts: conflicts.filter(c => c.severity === 'focus_block_conflict').length,
    timestamp: new Date().toISOString()
  };

  process.stdout.write(JSON.stringify(result));
  // Finding conflicts is a normal result, not a failure — the decision is
  // carried in has_conflicts; exit 0 so the workflow can gate on the JSON.
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

module.exports = { detectConflicts };

if (require.main === module) {
  main().catch(err => {
    process.stderr.write(err.message);
    process.exit(1);
  });
}
