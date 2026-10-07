/** Format a post's ISO `YYYY-MM-DD` date for display (UTC, e.g. "July 15, 2026"). */
export function formatPostDate(iso: string): string {
  return new Date(`${iso}T00:00:00Z`).toLocaleDateString("en-US", {
    year: "numeric",
    month: "long",
    day: "numeric",
    timeZone: "UTC",
  });
}
