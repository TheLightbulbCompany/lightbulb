// apps/frontend/src/lib/timeAgo.ts

// Ported from upstream Paperclip's timeAgo.ts (paperclip/ui/src/lib/timeAgo.ts)
// (MIT, (c) 2025 Paperclip AI).
//
// ISOL8-LOCAL EDIT: upstream's version is past-only — it subtracts and formats
// the result, so ANY future instant lands in the `< MINUTE` branch and renders
// as "just now". That is silently wrong wherever we show a scheduled time: an
// issue monitor's "Re-check {timeAgo(nextCheckAt)}" read "just now" for a check
// due next week. Future instants now format as "in 3d". Past behaviour is
// byte-identical, so every existing caller is unaffected.

const MINUTE = 60;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const WEEK = 7 * DAY;
const MONTH = 30 * DAY;

/** Magnitude of a gap, without direction — "3d", "2mo". */
function magnitude(seconds: number): string {
  if (seconds < HOUR) return `${Math.floor(seconds / MINUTE)}m`;
  if (seconds < DAY) return `${Math.floor(seconds / HOUR)}h`;
  if (seconds < WEEK) return `${Math.floor(seconds / DAY)}d`;
  if (seconds < MONTH) return `${Math.floor(seconds / WEEK)}w`;
  return `${Math.floor(seconds / MONTH)}mo`;
}

export function timeAgo(date: Date | string): string {
  const now = Date.now();
  const then = new Date(date).getTime();
  // An unparseable date must not render as "just now" — say nothing instead.
  if (Number.isNaN(then)) return "";
  const seconds = Math.round((now - then) / 1000);

  if (seconds >= MINUTE) return `${magnitude(seconds)} ago`;
  // Within a minute either side reads the same in both directions.
  if (seconds > -MINUTE) return "just now";
  return `in ${magnitude(-seconds)}`;
}
