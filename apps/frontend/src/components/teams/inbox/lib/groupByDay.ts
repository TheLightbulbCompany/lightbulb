/** Bucket a newest-first list into calendar days in the viewer's timezone.
 *  Today and yesterday get names; everything older gets a date. Items whose
 *  timestamp will not parse are dropped — a wrong day is worse than no row. */
export interface DayGroup<T> {
  label: string;
  items: T[];
}

const DATE_LABEL = new Intl.DateTimeFormat(undefined, {
  weekday: "short",
  month: "short",
  day: "numeric",
});

function dayKey(d: Date): string {
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

export function groupByDay<T>(
  items: readonly T[],
  getIso: (item: T) => string | null | undefined,
  now: Date = new Date(),
): DayGroup<T>[] {
  const todayKey = dayKey(now);
  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  const yesterdayKey = dayKey(yesterday);

  const order: string[] = [];
  const byKey = new Map<string, DayGroup<T>>();

  for (const item of items) {
    const iso = getIso(item);
    const when = iso ? new Date(iso) : null;
    if (!when || Number.isNaN(when.getTime())) continue;
    const key = dayKey(when);
    let bucket = byKey.get(key);
    if (!bucket) {
      bucket = {
        label:
          key === todayKey ? "Today" : key === yesterdayKey ? "Yesterday" : DATE_LABEL.format(when),
        items: [],
      };
      byKey.set(key, bucket);
      order.push(key);
    }
    bucket.items.push(item);
  }

  return order.map((key) => byKey.get(key)!);
}
