/** The clinic's timezone; the only place it is named. */
export const TZ = "America/Denver";

const parts = new Intl.DateTimeFormat("en-US", { timeZone: TZ, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
/** Clinic offset from UTC at instant `t`, in ms (negative for Denver). */
function offsetAt(t: number): number {
  const p = Object.fromEntries(parts.formatToParts(new Date(t)).map((x) => [x.type, x.value]));
  return Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second) - Math.floor(t / 1000) * 1000;
}

/** "YYYY-MM-DDTHH:mm" read as clinic wall-clock time, to the UTC instant; null when it is not a real date. */
export function clinicToUtc(s: string): Date | null {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(s)) return null;
  const wall = Date.parse(s + ":00Z");
  if (isNaN(wall) || new Date(wall).toISOString().slice(0, 16) !== s) return null;
  return new Date(wall - offsetAt(wall - offsetAt(wall))); // second pass settles instants near a DST change
}

export const fmtClinic = (d: Date) => new Intl.DateTimeFormat("en-US", { timeZone: TZ, dateStyle: "medium", timeStyle: "short" }).format(d);
