/** Compact relative timestamp. Accepts seconds or milliseconds since epoch. */
export function relativeTime(ts: number | undefined, now: number = Date.now()): string {
  if (!ts || ts <= 0) return "";
  const ms = ts < 1e12 ? ts * 1000 : ts;
  const minutes = Math.floor(Math.max(0, now - ms) / 60_000);
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d`;
  return new Date(ms).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
