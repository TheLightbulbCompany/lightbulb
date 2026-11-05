// Home's three lists, cut from the server's `shownGroup` (never re-derived), so the rail badge, a room's About dot
// and Home's rows all count the same thing. Pure, so it is tested on its own.

import type { TaskSummary } from "@lightbulb/shared";

/** Needs you = waiting on you + ready for review; Working = working, applying and watched; Done = the rest
 *  (finished, resolved, someone else's failure), newest first. Proposed requests are skipped. */
export function homeSections(ts: readonly TaskSummary[]): { needsYou: TaskSummary[]; working: TaskSummary[]; done: TaskSummary[] } {
  const needsYou: TaskSummary[] = [], working: TaskSummary[] = [], done: TaskSummary[] = [];
  for (const t of ts) {
    const g = t.shownGroup;
    if (g === "waiting_on_you" || g === "ready_for_review") needsYou.push(t);
    else if (g === "working" || g === "landing" || (g === "idle" && t.status === "idle" && t.watching)) working.push(t);
    else if (g === "idle" || g === "resolved") done.push(t);
  }
  done.sort((a, b) => Date.parse(b.updatedAt) - Date.parse(a.updatedAt));
  return { needsYou, working, done };
}
