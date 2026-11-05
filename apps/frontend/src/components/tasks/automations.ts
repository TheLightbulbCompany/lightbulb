"use client";

// Lane A's automation routes (wave2-A Task 6; spec 06 §5.1). Not on main yet: everything here is
// written to that contract, and the panel shows its empty state until the routes answer.

import { useCallback } from "react";
import useSWR, { useSWRConfig } from "swr";
import { useAuth, useOrganization } from "@clerk/nextjs";
import { canDecide, canEdit, runNow as runNowShared, type AutomationView, type RunNowOutcome, type TaskSummary } from "@lightbulb/shared";
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

/** Who may turn a proposal on or resume it (shared `canDecide`, the server's two predicates). */
export function useCanDecide(): (a: Pick<AutomationView, "deliverTo" | "ownerUserId" | "match">) => boolean {
  const { userId } = useAuth();
  const { membership } = useOrganization();
  const admin = membership?.role === "org:admin";
  return useCallback((a) => canDecide(a, { userId: userId ?? null, admin }), [admin, userId]);
}

/** Who may pause it, run it now or drop a proposal (the server's `_may_edit`): an org admin or its owner. */
export function useCanEdit(): (a: Pick<AutomationView, "ownerUserId">) => boolean {
  const { userId } = useAuth();
  const { membership } = useOrganization();
  const admin = membership?.role === "org:admin";
  return useCallback((a) => canEdit(a, { userId: userId ?? null, admin }), [admin, userId]);
}

export type { RunNowOutcome };
export const runNow = (act: (id: string, verb: AutomationVerb) => Promise<unknown>, a: Pick<AutomationView, "id" | "activeRunTaskId" | "status">): Promise<RunNowOutcome> =>
  runNowShared((id) => act(id, "run"), a, errorCopy);

export { budgetLine, deliveryWords, money, runNowReason, runsPerMonth, scheduleWords, viewerTimeZone, waitingWords } from "@lightbulb/shared";
