"use client";

// Lane A's automation routes (wave2-A Task 6; spec 06 §5.1). Not on main yet: everything here is
// written to that contract, and the panel shows its empty state until the routes answer.

import { useCallback } from "react";
import useSWR, { useSWRConfig } from "swr";
import { useAuth, useOrganization } from "@clerk/nextjs";
import { ApiError, formatSchedule, type AutomationView, type TaskSummary } from "@lightbulb/shared";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { useApi } from "@/lib/api";
import { errorCopy } from "./useTaskActions";

export type AutomationVerb = "activate" | "pause" | "resume" | "run";

export function useAutomation(id: string | null) {
  const api = useApi();
  const { tasks } = useLiveEvents();
  return useSWR<AutomationView>(id ? ["automation", tasks.cacheScope, id] : null,
    () => api.get(`/automations/${encodeURIComponent(id!)}`) as Promise<AutomationView>, { shouldRetryOnError: false });
}

export function useAutomationRuns(id: string | null) {
  const api = useApi();
  const { tasks } = useLiveEvents();
  return useSWR<{ runs: TaskSummary[]; next: string | null }>(id ? ["automation-runs", tasks.cacheScope, id] : null,
    () => api.get(`/automations/${encodeURIComponent(id!)}/runs`) as Promise<{ runs: TaskSummary[]; next: string | null }>, { shouldRetryOnError: false });
}

/** One POST per press; the list, the row and its runs re-read afterwards. */
export function useAutomationActions() {
  const api = useApi();
  const { tasks } = useLiveEvents();
  const { mutate } = useSWRConfig();
  const refresh = useCallback((id: string) => Promise.all([
    mutate(["automations", tasks.cacheScope]), mutate(["automation", tasks.cacheScope, id]), mutate(["automation-runs", tasks.cacheScope, id]),
  ]), [mutate, tasks.cacheScope]);
  const act = useCallback(async (id: string, verb: AutomationVerb): Promise<AutomationView | { triggerId: string }> => {
    const res = await api.post(`/automations/${encodeURIComponent(id)}/${verb}`, {}) as AutomationView | { triggerId: string };
    await refresh(id);
    return res;
  }, [api, refresh]);
  const remove = useCallback(async (id: string) => {
    await api.del(`/automations/${encodeURIComponent(id)}`);
    await refresh(id);
  }, [api, refresh]);
  return { act, remove, refresh };
}

/** The schedule in the viewer's zone, and the row's own zone named when it differs: "Mondays 14:00 (New York time: Mondays 9:00)". */
export function scheduleWords(a: Pick<AutomationView, "schedule">, viewerTz: string): string | null {
  if (!a.schedule) return null;
  const words = formatSchedule(a.schedule.cron, a.schedule.tz, viewerTz);
  if (a.schedule.tz === viewerTz) return words;
  const zone = a.schedule.tz.split("/").pop()?.replace(/_/g, " ") ?? a.schedule.tz;
  return `${words} (${zone} time: ${formatSchedule(a.schedule.cron, a.schedule.tz, a.schedule.tz)})`;
}

/** Where a run posts, in words (never the raw target id). */
export function deliveryWords(deliverTo: string): string | null {
  if (!deliverTo || deliverTo === "none") return null;
  return deliverTo.startsWith("room:") ? "posts in a room" : "posts in a DM";
}

/** Who may turn a proposal on or resume it (routers/automations.py `activate`): a room-delivered one needs an org
 *  admin, a personal one its owner. */
export function useCanDecide(): (a: Pick<AutomationView, "deliverTo" | "ownerUserId">) => boolean {
  const { userId } = useAuth();
  const { membership } = useOrganization();
  const admin = membership?.role === "org:admin";
  return useCallback((a) => (a.deliverTo.startsWith("room:") ? admin : a.ownerUserId === userId), [admin, userId]);
}

/** Who may pause it, run it now or drop a proposal (the server's `_may_edit`): an org admin or its owner. */
export function useCanEdit(): (a: Pick<AutomationView, "ownerUserId">) => boolean {
  const { userId } = useAuth();
  const { membership } = useOrganization();
  const admin = membership?.role === "org:admin";
  return useCallback((a) => admin || a.ownerUserId === userId, [admin, userId]);
}

/** The line others read while they can't turn it on. */
export const waitingWords = (a: Pick<AutomationView, "deliverTo">): string =>
  a.deliverTo.startsWith("room:") ? "Waiting on a workspace admin" : "Waiting on its owner";

export type RunNowOutcome = { kind: "started" } | { kind: "running"; taskId: string } | { kind: "note"; message: string };

/** Run now: one POST; a 409 run_in_progress names the run to open; not_active and the gate carry their own words. */
export async function runNow(act: (id: string, verb: AutomationVerb) => Promise<unknown>, a: Pick<AutomationView, "id" | "activeRunTaskId" | "status">): Promise<RunNowOutcome> {
  if (a.activeRunTaskId) return { kind: "running", taskId: a.activeRunTaskId };
  if (a.status === "draft") return { kind: "note", message: "Turn it on first." };
  try {
    await act(a.id, "run");
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

export const money = (n: number): string => `$${n.toFixed(2).replace(/\.00$/, "")}`;

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
