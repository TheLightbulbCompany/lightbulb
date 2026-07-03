/**
 * Relative-time formatting for admin timestamps.
 *
 * Pure function of (value, now) so it's unit-testable and safe in Server
 * Components (they render at request time; the admin pages refresh often
 * enough that a request-time "3m ago" stays honest). Callers must put the
 * returned `iso` in a `title` (and `dateTime` where applicable) attribute so
 * the absolute timestamp is always one hover away.
 */

export interface RelativeTimeParts {
  /** "just now", "3m ago", "2d ago", the date for >7d, or "—" when absent. */
  label: string;
  /** Absolute ISO-8601 string for title/dateTime attributes; null when unparseable. */
  iso: string | null;
}

const MINUTE_MS = 60_000;
const HOUR_MS = 3_600_000;
const DAY_MS = 86_400_000;

/**
 * Accepts ISO strings (DDB rows) or ms-since-epoch numbers (Clerk) and
 * degrades to the raw value when unparseable rather than throwing.
 */
export function formatRelativeTime(
  value: string | number | null | undefined,
  now: number = Date.now(),
): RelativeTimeParts {
  if (value === null || value === undefined || value === "") {
    return { label: "—", iso: null };
  }

  const ms = typeof value === "number" ? value : Date.parse(value);
  if (!Number.isFinite(ms)) {
    return { label: String(value), iso: null };
  }

  const iso = new Date(ms).toISOString();
  const diff = now - ms;

  // Future timestamps (clock skew, period-end dates) fall back to the date;
  // a fabricated "-3m ago" would read as data corruption to an operator.
  if (diff < -MINUTE_MS) {
    return { label: iso.slice(0, 10), iso };
  }
  if (diff < MINUTE_MS) {
    return { label: "just now", iso };
  }
  if (diff < HOUR_MS) {
    return { label: `${Math.floor(diff / MINUTE_MS)}m ago`, iso };
  }
  if (diff < DAY_MS) {
    return { label: `${Math.floor(diff / HOUR_MS)}h ago`, iso };
  }
  if (diff <= 7 * DAY_MS) {
    return { label: `${Math.floor(diff / DAY_MS)}d ago`, iso };
  }
  return { label: iso.slice(0, 10), iso };
}
