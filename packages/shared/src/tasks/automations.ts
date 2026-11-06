// Words for an automation row (lane A's `AutomationView`), shared by web and mobile.

import { ApiError } from "../api/error";
import type { AutomationView } from "./types";
import { CUSTOM_SCHEDULE, formatSchedule } from "./view";

export const money = (n: number): string => `$${n.toFixed(2).replace(/\.00$/, "")}`;

/** "America/New_York" → "New York time"; UTC is just "UTC". */
const zoneName = (tz: string): string => /^(Etc\/)?(UTC|GMT|Universal|Zulu)$/.test(tz) ? "UTC" : `${tz.split("/").pop()?.replace(/_/g, " ") ?? tz} time`;

/** The schedule with its time zone always named (dev walk W8): "Mondays 9:00 New York time" in the row's own zone, or the
 *  viewer's time with the row's zone beside it when they differ: "Mondays 14:00 (New York time: Mondays 9:00)". */
export function scheduleWords(a: Pick<AutomationView, "schedule">, viewerTz: string, now = Date.now()): string | null {
  if (!a.schedule) return null;
  const words = formatSchedule(a.schedule.cron, a.schedule.tz, viewerTz, now);
  if (words === CUSTOM_SCHEDULE) return words;
  const zone = zoneName(a.schedule.tz);
  if (a.schedule.tz === viewerTz) return `${words} ${zone}`;
  return `${words} (${zone}: ${formatSchedule(a.schedule.cron, a.schedule.tz, a.schedule.tz, now)})`;
}

/** Where a run posts, in words (never the raw target id): "posts to #walk-8" when the room's label is known. */
export function deliveryWords(deliverTo: string, roomLabel?: string | null): string | null {
  if (!deliverTo || deliverTo === "none") return null;
  if (!deliverTo.startsWith("room:")) return "posts in a DM";
  return roomLabel ? `posts to ${roomLabel}` : "posts in a room";
}

/** The room id a `room:<id>` target names, else null. */
export const deliveryRoomId = (deliverTo: string): string | null => deliverTo.startsWith("room:") ? deliverTo.slice("room:".length) : null;

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

/** A next-run time in the viewer's zone: "Mon 10 Nov, 9:00", with the year when it isn't this year ("Mon, 4 Jan 2027, 9:00"). */
export function nextRunWords(iso: string, viewerTz: string, now = Date.now()): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const year = (t: Date) => new Intl.DateTimeFormat("en-US", { timeZone: viewerTz, year: "numeric" }).format(t);
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: viewerTz, weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit", hourCycle: "h23",
    ...(year(d) !== year(new Date(now)) ? { year: "numeric" as const } : {}),
  }).format(d);
}

/** A Scheduled row's line: always a status ("On", "Paused", "Not turned on"), its schedule ("Mondays 9:00", or
 *  "runs on a match" for a rule), then while On when it runs next ("next Mon 9 Nov, 9:00"; a one-off names it already). */
export function automationRowLine(
  a: Pick<AutomationView, "status" | "pausedReason" | "schedule" | "nextRuns" | "match">, viewerTz: string,
): string {
  const status = automationStatusLine(a) ?? "On";
  const when = scheduleWords(a, viewerTz) ?? (a.match ? "runs on a match" : null);
  const words = a.status === "active" && a.nextRuns?.[0] && !when?.startsWith("Once") ? nextRunWords(a.nextRuns[0], viewerTz) : "";
  const next = words ? `next ${words}` : null;
  return [status, when, next].filter(Boolean).join(" · ");
}
