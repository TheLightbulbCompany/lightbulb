#!/usr/bin/env node
/**
 * pulse-calendar-conflict-checker.js
 * Requirements 17, 27: Flag content near sensitive dates. Daily + pre-approval.
 *
 * Deterministic date matching. News cycle data passed in from pipeline search step.
 */

// Seeded cultural calendar — expanded by marketer over time
const DEFAULT_CULTURAL_CALENDAR = [
  { date: '01-27', name: 'International Holocaust Remembrance Day' },
  { date: '02-01', name: 'Start of Black History Month' },
  { date: '03-08', name: 'International Women\'s Day' },
  { date: '03-17', name: 'St. Patrick\'s Day' },
  { date: '04-22', name: 'Earth Day' },
  { date: '05-01', name: 'International Workers\' Day' },
  { date: '05-27', name: 'Memorial Day (approx)' },
  { date: '06-01', name: 'Start of Pride Month' },
  { date: '06-19', name: 'Juneteenth' },
  { date: '07-04', name: 'US Independence Day' },
  { date: '09-11', name: 'September 11 Anniversary' },
  { date: '10-01', name: 'Start of Hispanic Heritage Month' },
  { date: '11-09', name: 'Anniversary of Kristallnacht' },
  { date: '11-11', name: 'Veterans Day / Remembrance Day' },
  { date: '12-25', name: 'Christmas Day' }
];

function checkCalendarConflicts(scheduledContent, culturalCalendar, newsConflicts) {
  const calendar = [...DEFAULT_CULTURAL_CALENDAR, ...(culturalCalendar || [])];
  const conflicts = [];

  for (const content of scheduledContent) {
    const publishDate = new Date(content.scheduled_date);
    const pubMonth = String(publishDate.getMonth() + 1).padStart(2, '0');
    const pubDay = String(publishDate.getDate()).padStart(2, '0');
    const pubDateStr = `${pubMonth}-${pubDay}`;

    // Check ±2 days around cultural dates
    for (const event of calendar) {
      const [eventMonth, eventDay] = event.date.split('-').map(Number);
      const eventDate = new Date(publishDate.getFullYear(), eventMonth - 1, eventDay);
      const daysDiff = Math.abs((publishDate - eventDate) / (1000 * 60 * 60 * 24));

      if (daysDiff <= 2) {
        conflicts.push({
          content_id: content.id,
          content_title: content.title,
          scheduled_date: content.scheduled_date,
          conflict_type: 'cultural_date',
          event_name: event.name,
          event_date: event.date,
          days_from_event: Math.round(daysDiff),
          severity: daysDiff === 0 ? 'high' : 'medium',
          message: daysDiff === 0
            ? `Scheduled on ${event.name}. Review content for contextual sensitivity.`
            : `Scheduled ${Math.round(daysDiff)} day(s) from ${event.name}. Check for potential insensitivity.`
        });
      }
    }

    // Check news cycle conflicts (passed in from Perplexity search results)
    if (newsConflicts && newsConflicts.length > 0) {
      for (const news of newsConflicts) {
        if (news.relevant_to_content) {
          conflicts.push({
            content_id: content.id,
            content_title: content.title,
            scheduled_date: content.scheduled_date,
            conflict_type: 'news_cycle',
            news_topic: news.topic,
            severity: 'medium',
            message: `Active news cycle: "${news.topic}" — review whether scheduled content could clash or if timing should shift.`
          });
        }
      }
    }
  }

  return {
    has_conflicts: conflicts.length > 0,
    conflicts,
    content_checked: scheduledContent.length,
    high_severity: conflicts.filter(c => c.severity === 'high').length,
    timestamp: new Date().toISOString()
  };
}

async function main() {
  const input = await readStdin();
  if (!input) { process.stderr.write('No input'); process.exit(1); }

  const result = checkCalendarConflicts(
    input.scheduled_content || [],
    input.cultural_calendar || [],
    input.news_conflicts || []
  );

  process.stdout.write(JSON.stringify(result));
  // A conflict is a normal review outcome, not a failure — the workflow branches on
  // result.has_conflicts via condition:, so exit 0 and let the caller gate.
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

module.exports = { checkCalendarConflicts };

if (require.main === module) { main().catch(err => { process.stderr.write(err.message); process.exit(1); }); }
