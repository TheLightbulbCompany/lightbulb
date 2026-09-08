import type { HeartbeatRun } from "@/components/teams/shared/types";

/** One collapsed group of identical failed runs.
 *
 * A flapping heartbeat emits the same failure once per occurrence — on prod
 * a single agent produced 492 identical rows. We collapse by
 * ``(agentId + failureReason)`` (NOT ``run.id`` / ``startedAt``, which are
 * per-occurrence) so the band shows ONE row per distinct failure with a
 * count. ``representative`` is the most-recent member (drives the title +
 * timestamp + View link); ``runs`` is every member so a dismiss can ack
 * them all. */
export interface FailedRunGroup {
  key: string;
  representative: HeartbeatRun;
  runs: HeartbeatRun[];
  count: number;
}

/** Most-recent-first comparator on ``startedAt`` (missing sorts last). */
function startedAtDesc(a: HeartbeatRun, b: HeartbeatRun): number {
  const ta = a.startedAt ? Date.parse(a.startedAt) : NaN;
  const tb = b.startedAt ? Date.parse(b.startedAt) : NaN;
  if (Number.isNaN(ta) && Number.isNaN(tb)) return 0;
  if (Number.isNaN(ta)) return 1;
  if (Number.isNaN(tb)) return -1;
  return tb - ta;
}

/** Collapse identical failed runs into groups keyed by agent + reason.
 *
 * Group order follows first-appearance in ``failedRuns`` (the upstream feed
 * is already newest-first); within a group the representative is the
 * most-recent occurrence so its timestamp + run-detail link stay live. */
export function groupFailedRuns(failedRuns: HeartbeatRun[]): FailedRunGroup[] {
  const byKey = new Map<string, HeartbeatRun[]>();
  const order: string[] = [];
  for (const run of failedRuns) {
    const key = JSON.stringify([run.agentId ?? "", run.failureReason ?? ""]);
    const bucket = byKey.get(key);
    if (bucket) {
      bucket.push(run);
    } else {
      byKey.set(key, [run]);
      order.push(key);
    }
  }
  return order.map((key) => {
    const runs = byKey.get(key)!;
    const representative = runs.reduce((latest, r) =>
      startedAtDesc(r, latest) < 0 ? r : latest,
    );
    return { key, representative, runs, count: runs.length };
  });
}
