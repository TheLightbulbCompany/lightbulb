// Words for an automation row (lane A's `AutomationView`), shared by web and mobile.

import { ApiError } from "../api/error";
import type { AutomationView } from "./types";
import { formatSchedule } from "./view";

export const money = (n: number): string => `$${n.toFixed(2).replace(/\.00$/, "")}`;

/** The schedule in the viewer's zone, and the row's own zone named when it differs: "Mondays 14:00 (New York time: Mondays 9:00)". */
export function scheduleWords(a: Pick<AutomationView, "schedule">, viewerTz: string, now = Date.now()): string | null {
  if (!a.schedule) return null;
  const words = formatSchedule(a.schedule.cron, a.schedule.tz, viewerTz, now);
  if (a.schedule.tz === viewerTz) return words;
  const zone = a.schedule.tz.split("/").pop()?.replace(/_/g, " ") ?? a.schedule.tz;
  return `${words} (${zone} time: ${formatSchedule(a.schedule.cron, a.schedule.tz, a.schedule.tz, now)})`;
}

/** Where a run posts, in words (never the raw target id). */
export function deliveryWords(deliverTo: string): string | null {
  if (!deliverTo || deliverTo === "none") return null;
  return deliverTo.startsWith("room:") ? "posts in a room" : "posts in a DM";
}

/** Roughly how often a cron list fires a month (every day 30, weekdays 22, N named days 4×N); null when it can't say. */
export function runsPerMonth(cron: readonly string[]): number | null {
  let total = 0;
  for (const entry of cron) {
    const [, , dom, mon, dow = "*"] = entry.trim().split(/\s+/);
    if (dom !== "*" || mon !== "*") return null;
    if (dow === "*") total += 30;
    else if (dow === "1-5") total += 22;
    else if (/^[\w,]+$/.test(dow)) total += 4 * dow.split(",").length;
    else return null;
  }
  return total || null;
}

/** "up to $1 a run, about $4 a month" from the row's budget and schedule. */
export function budgetLine(a: Pick<AutomationView, "budgetPerRunUsd" | "schedule">): string | null {
  if (a.budgetPerRunUsd == null) return null;
  const perMonth = a.schedule ? runsPerMonth(a.schedule.cron) : null;
  return `up to ${money(a.budgetPerRunUsd)} a run${perMonth ? `, about ${money(a.budgetPerRunUsd * perMonth)} a month` : ""}`;
}

/** "Paused: <reason>" / "Paused" / "Not turned on", else null. */
export function automationStatusLine(a: Pick<AutomationView, "status" | "pausedReason">): string | null {
  if (a.status === "paused" || a.status === "paused_system") return a.pausedReason ? `Paused: ${a.pausedReason}` : "Paused";
  if (a.status === "draft") return "Not turned on";
  return null;
}

type DecideRow = Pick<AutomationView, "deliverTo" | "ownerUserId" | "match">;

/** Whose call turning it on is (routers/automations.py): a rule by its match (calendar → its owner, else an org
 *  admin, `_may_turn_on_rule`); anything else by where it posts (room → an org admin, else its owner, `_may_activate`). */
const needsAdmin = (a: Pick<AutomationView, "deliverTo" | "match">): boolean =>
  a.match ? !(a.match.sources ?? []).includes("calendar") : a.deliverTo.startsWith("room:");

/** The line others read while they can't turn it on. */
export const waitingWords = (a: Pick<AutomationView, "deliverTo" | "match">): string =>
  needsAdmin(a) ? "Waiting on a workspace admin" : "Waiting on its owner";

/** Turn on / Resume. */
export const canDecide = (a: DecideRow, viewer: { userId: string | null; admin: boolean }): boolean =>
  needsAdmin(a) ? viewer.admin : a.ownerUserId === viewer.userId;

/** Why Run now is off, or null when it may run (the server's `run_now`: `_may_edit`, then status must be active). */
export function runNowReason(a: Pick<AutomationView, "status">, mayEdit: boolean): string | null {
  if (!mayEdit) return "Only its owner or an admin can run it";
  if (a.status === "draft") return "Turn it on first";
  if (a.status !== "active") return "Resume it first";
  return null;
}

/** Pause / Run now / Not now (the server's `_may_edit`): an org admin or the owner. */
export const canEdit = (a: Pick<AutomationView, "ownerUserId">, viewer: { userId: string | null; admin: boolean }): boolean =>
  viewer.admin || a.ownerUserId === viewer.userId;

export type RunNowOutcome = { kind: "started" } | { kind: "running"; taskId: string } | { kind: "note"; message: string };

/** Run now: one POST; a 409 run_in_progress names the run to open; not_active carries its own words; anything
 *  else reads through the app's `errorCopy`. */
export async function runNow(
  run: (id: string) => Promise<unknown>, a: Pick<AutomationView, "id" | "activeRunTaskId" | "status">, errorCopy: (error: unknown) => string,
): Promise<RunNowOutcome> {
  if (a.activeRunTaskId) return { kind: "running", taskId: a.activeRunTaskId };
  if (a.status === "draft") return { kind: "note", message: "Turn it on first." };
  try {
    await run(a.id);
    return { kind: "started" };
  } catch (error) {
    if (error instanceof ApiError) {
      const detail = (error.body as { detail?: Record<string, unknown> } | null)?.detail ?? (error.body as Record<string, unknown> | null);
      const code = typeof detail?.code === "string" ? detail.code : null;
      if (error.status === 409 && code === "run_in_progress" && typeof detail?.taskId === "string") return { kind: "running", taskId: detail.taskId };
      if (code === "not_active") return { kind: "note", message: "Turn it on first." };
    }
    return { kind: "note", message: errorCopy(error) };
  }
}

export const viewerTimeZone = (): string => {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch { return "UTC"; }
};
