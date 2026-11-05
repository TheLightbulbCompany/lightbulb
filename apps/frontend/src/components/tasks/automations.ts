"use client";

// Lane A's automation routes (routers/automations.py): actions and the viewer's two predicates. Reads go
// through the shared hooks (`useAutomations(useAutomationSource())`).

import { useCallback } from "react";
import { useSWRConfig } from "swr";
import { useAuth, useOrganization } from "@clerk/nextjs";
import {
  automationKey, automationRunsKey, automationsKey, canDecide, canEdit, runNow as runNowShared,
  type AutomationSource, type AutomationView, type RunNowOutcome,
} from "@lightbulb/shared";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { useApi } from "@/lib/api";
import { errorCopy } from "./useTaskActions";

export type AutomationVerb = "activate" | "pause" | "resume" | "run";

/** What the shared automation hooks read through: the signed-in API client and the workspace scope. */
export function useAutomationSource(): AutomationSource {
  return { api: useApi(), cacheScope: useLiveEvents().tasks.cacheScope };
}

/** One POST per press; the list, the row and its runs re-read afterwards. */
export function useAutomationActions() {
  const api = useApi();
  const { tasks } = useLiveEvents();
  const { mutate } = useSWRConfig();
  const refresh = useCallback((id: string) => Promise.all([
    mutate(automationsKey(tasks.cacheScope)), mutate(automationKey(tasks.cacheScope, id)), mutate(automationRunsKey(tasks.cacheScope, id)),
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
