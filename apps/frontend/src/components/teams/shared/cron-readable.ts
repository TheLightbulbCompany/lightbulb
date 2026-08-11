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

  // Handle hourly: "0 * * * *"
  if (minute === "0" && hour === "*" && dayOfMonth === "*" && month === "*" && dayOfWeek === "*") {
    return "Every hour";
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

  const hh = String(h).padStart(2, "0");
  const mm = String(parseInt(minute, 10)).padStart(2, "0");
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
