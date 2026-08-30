/**
 * America/New_York <-> `<input type="datetime-local">` conversion for the
 * launch settings card. The settings row stores ISO-8601 with an explicit
 * offset (`core.services.launch_settings._validate_date` requires one); the
 * admin edits wall-clock ET, so both directions live here rather than
 * duplicated across the card's two date fields.
 */

const ET_ZONE = "America/New_York";

/** ISO string (any offset) -> "YYYY-MM-DDTHH:mm" ET wall time, for the input
 * value. `null`/`""` -> `""` (empty input). */
export function toEasternLocal(iso: string | null): string {
  if (!iso) return "";
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: ET_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    // `hourCycle: "h23"`, not `hour12: false` — en-US with `hour12:false` can
    // still resolve to `h24` (midnight rendered as "24:00"), which is not a
    // valid `datetime-local` input value. h23 pins midnight to "00".
    hourCycle: "h23",
  }).formatToParts(new Date(iso));
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "00";
  return `${get("year")}-${get("month")}-${get("day")}T${get("hour")}:${get("minute")}`;
}

/** ET offset in effect at a given instant, as `"-04:00"` / `"-05:00"`. */
function etOffsetAt(instant: Date): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: ET_ZONE,
    timeZoneName: "longOffset",
  }).formatToParts(instant);
  const gmtOffset = parts.find((p) => p.type === "timeZoneName")?.value ?? "GMT-05:00";
  return gmtOffset.replace("GMT", "") || "+00:00";
}

/** `"-04:00"` -> its signed millisecond value. */
function offsetMs(offset: string): number {
  const m = /^([+-])(\d{2}):(\d{2})$/.exec(offset);
  if (!m) return 0;
  return (m[1] === "-" ? -1 : 1) * (Number(m[2]) * 60 + Number(m[3])) * 60_000;
}

/** "YYYY-MM-DDTHH:mm" ET wall time -> ISO string with the correct ET offset
 * for that wall time (`-04:00` EDT / `-05:00` EST).
 *
 * DST resolution takes two passes, because reading the offset once is wrong
 * for the 4-5 hours after a transition: the wall time treated as a UTC
 * instant lands BEFORE the transition (ET is 4-5 hours behind UTC), so a
 * single read reports the old season. So: read the offset at the naive
 * instant, subtract it to get the UTC instant that offset implies, and read
 * the offset there — `2026-11-01T03:00` resolves to EST, `2027-03-14T05:00`
 * to EDT.
 *
 * The second read is then verified by converting back: a spring-forward gap
 * wall time (e.g. 2026-03-08T02:30, which never occurs on a real ET clock)
 * doesn't render back as itself, and keeps the pre-transition offset (EST) —
 * a documented choice, not a crash. An ambiguous fall-back wall time (e.g.
 * 2026-11-01T01:30, which occurs twice) resolves to the FIRST occurrence,
 * EDT. */
export function fromEasternLocal(local: string): string {
  const [datePart, timePart] = local.split("T");
  const [year, month, day] = datePart.split("-").map(Number);
  const [hour, minute] = timePart.split(":").map(Number);
  const naive = Date.UTC(year, month - 1, day, hour, minute);
  const first = etOffsetAt(new Date(naive));
  const second = etOffsetAt(new Date(naive - offsetMs(first)));
  const roundTrips = toEasternLocal(new Date(naive - offsetMs(second)).toISOString()) === local;
  return `${local}:00${roundTrips ? second : first}`;
}
