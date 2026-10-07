/**
 * Convert a cron expression to human-readable text.
 * Returns e.g. "Every day at 08:00", "Every Monday at 08:00", "Every hour".
 * Falls back to the raw expression if it can't parse.
 *
 * Only handles common shapes: fixed hour+minute daily, single/day-range DOW, hourly.
 * Cron format: minute hour dayOfMonth month dayOfWeek
 */
export function describeCron(expr: string): string {
  const parts = expr.split(/\s+/);
  if (parts.length !== 5) {
    return expr; // fallback to raw
  }

  const [minute, hour, dayOfMonth, month, dayOfWeek] = parts;

  // Handle hourly: "0 * * * *" (and "15 * * * *" -> "Every hour at :15")
  if (hour === "*" && dayOfMonth === "*" && month === "*" && dayOfWeek === "*") {
    if (minute === "0") return "Every hour";
    if (isFixedNumber(minute)) {
      const m = parseInt(minute, 10);
      if (m >= 0 && m <= 59) return `Every hour at :${String(m).padStart(2, "0")}`;
    }
    return expr;
  }

  // Only handle fixed minute+hour, all days+months
  if (dayOfMonth !== "*" || month !== "*") {
    return expr; // too complex
  }

  // Hour must be a fixed number
  if (!isFixedNumber(hour)) {
    return expr;
  }

  // Minute must be a fixed number
  if (!isFixedNumber(minute)) {
    return expr;
  }

  const h = parseInt(hour, 10);
  if (isNaN(h) || h < 0 || h > 23) {
    return expr;
  }

  const m = parseInt(minute, 10);
  if (isNaN(m) || m < 0 || m > 59) {
    return expr;
  }

  const hh = String(h).padStart(2, "0");
  const mm = String(m).padStart(2, "0");
  const timeStr = `${hh}:${mm}`;

  // Parse dayOfWeek
  if (dayOfWeek === "*") {
    // Every day
    return `Every day at ${timeStr}`;
  }

  // Single day: "1" = Monday
  if (/^\d+$/.test(dayOfWeek)) {
    const dow = parseInt(dayOfWeek, 10);
    const dayName = getDayName(dow);
    if (dayName) {
      return `Every ${dayName} at ${timeStr}`;
    }
    return expr;
  }

  // Day range: "1-5" = Monday through Friday
  if (/^\d+-\d+$/.test(dayOfWeek)) {
    const [start, end] = dayOfWeek.split("-").map((x) => parseInt(x, 10));
    if (start === 1 && end === 5) {
      return `Every weekday at ${timeStr}`;
    }
    return expr;
  }

  // List or complex: fallback
  return expr;
}

/**
 * The schedule shapes the human picker can express — deliberately the SAME set
 * `describeCron` above renders as a sentence, so anything the picker builds
 * reads back as friendly text in the automations list, and anything the list
 * shows as friendly text can be reopened in the picker. Everything else is
 * cron's business: `parseCron` returns null for it and the picker steps aside
 * for its raw-expression field rather than rewriting someone's schedule.
 */
export interface Schedule {
  freq: "hourly" | "daily" | "weekdays" | "weekly";
  hour: number;
  minute: number;
  /** Day of week, 0=Sunday. Only meaningful for `weekly`. */
  day: number;
}

export function buildCron(s: Schedule): string {
  if (s.freq === "hourly") return `${s.minute} * * * *`;
  const dow = s.freq === "daily" ? "*" : s.freq === "weekdays" ? "1-5" : String(s.day);
  return `${s.minute} ${s.hour} * * ${dow}`;
}

/** Inverse of `buildCron`. Null when the expression is outside the picker's
 * vocabulary (day-of-month, step values, lists, ranges other than 1-5). */
export function parseCron(expr: string): Schedule | null {
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const [minute, hour, dayOfMonth, month, dayOfWeek] = parts;
  if (dayOfMonth !== "*" || month !== "*") return null;
  if (!isFixedNumber(minute)) return null;
  const m = parseInt(minute, 10);
  if (m < 0 || m > 59) return null;

  if (hour === "*") {
    return dayOfWeek === "*" ? { freq: "hourly", hour: 0, minute: m, day: 1 } : null;
  }
  if (!isFixedNumber(hour)) return null;
  const h = parseInt(hour, 10);
  if (h < 0 || h > 23) return null;

  if (dayOfWeek === "*") return { freq: "daily", hour: h, minute: m, day: 1 };
  if (dayOfWeek === "1-5") return { freq: "weekdays", hour: h, minute: m, day: 1 };
  if (isFixedNumber(dayOfWeek)) {
    const d = parseInt(dayOfWeek, 10);
    if (d >= 0 && d <= 6) return { freq: "weekly", hour: h, minute: m, day: d };
  }
  return null;
}

function isFixedNumber(s: string): boolean {
  return /^\d+$/.test(s);
}

function getDayName(dow: number): string | null {
  const names: Record<number, string> = {
    0: "Sunday",
    1: "Monday",
    2: "Tuesday",
    3: "Wednesday",
    4: "Thursday",
    5: "Friday",
    6: "Saturday",
  };
  return names[dow] ?? null;
}
