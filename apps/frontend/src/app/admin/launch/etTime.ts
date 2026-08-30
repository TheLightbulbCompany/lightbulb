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
    hour12: false,
  }).formatToParts(new Date(iso));
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "00";
  // Some ICU builds render midnight as "24" under hour12:false — normalize.
  const hour = get("hour") === "24" ? "00" : get("hour");
  return `${get("year")}-${get("month")}-${get("day")}T${hour}:${get("minute")}`;
}

/** "YYYY-MM-DDTHH:mm" ET wall time -> ISO string with the correct ET offset
 * for that wall time (`-04:00` EDT / `-05:00` EST).
 *
 * DST resolution: the wall time is first treated as if it were a UTC
 * instant, and the ET formatter is asked what offset America/New_York uses
 * AT THAT (UTC) INSTANT. Because ET is 4-5 hours behind UTC, that instant
 * falls on the same calendar date, so it reads the correct season almost
 * always — the only place it can disagree with "the offset ET actually used
 * at that wall clock" is the ~1-5 hour window around a DST transition itself,
 * which is exactly the ambiguous/nonexistent wall time this function has no
 * single correct answer for anyway. For the spring-forward gap (e.g.
 * 2026-03-08T02:30, which never occurs on a real ET clock) this resolves to
 * the PRE-transition offset (EST, -05:00) rather than throwing — a documented
 * choice, not a crash. */
export function fromEasternLocal(local: string): string {
  const [datePart, timePart] = local.split("T");
  const [year, month, day] = datePart.split("-").map(Number);
  const [hour, minute] = timePart.split(":").map(Number);
  const naiveInstant = new Date(Date.UTC(year, month - 1, day, hour, minute));
  const offsetParts = new Intl.DateTimeFormat("en-US", {
    timeZone: ET_ZONE,
    timeZoneName: "longOffset",
  }).formatToParts(naiveInstant);
  const gmtOffset = offsetParts.find((p) => p.type === "timeZoneName")?.value ?? "GMT-05:00";
  const offset = gmtOffset.replace("GMT", "") || "+00:00";
  return `${local}:00${offset}`;
}
