// Lane A's automation routes on the phone (the web twin: components/tasks/automations.ts).

import { useCallback } from "react";
import { useAuth, useOrganization } from "@clerk/expo";
import {
  automationKey, automationRunsKey, automationsKey, runNow as runNowShared, type AutomationSource,
  useTaskCache, type AutomationView,
} from "@lightbulb/shared";

import { useLiveEvents } from "../../providers/live-events";
import { errorCopy } from "./useTaskActions";

export type AutomationVerb = "activate" | "pause" | "resume" | "run";

/** What the shared automation hooks read through: this phone's API client and workspace scope. */
export function useAutomationSource(): AutomationSource {
  const { api, tasks } = useLiveEvents();
  return { api, cacheScope: tasks.cacheScope };
}

export function useAutomationActions() {
  const { api, tasks } = useLiveEvents();
  const { mutate } = useTaskCache();
  const refresh = useCallback((id: string) => Promise.all([
    mutate(automationsKey(tasks.cacheScope)), mutate(automationKey(tasks.cacheScope, id)), mutate(automationRunsKey(tasks.cacheScope, id)),
  ]), [mutate, tasks.cacheScope]);
  const act = useCallback(async (id: string, verb: AutomationVerb) => {
    const res = await api.post(`/automations/${encodeURIComponent(id)}/${verb}`, {});
    await refresh(id);
    return res;
  }, [api, refresh]);
  return { act, refresh };
}

/** The viewer as the server's two predicates see them (routers/automations.py). */
export function useAutomationViewer(): { userId: string | null; admin: boolean } {
  const { userId } = useAuth();
  const { membership } = useOrganization();
  return { userId: userId ?? null, admin: membership?.role === "org:admin" };
}

export const runNow = (act: (id: string, verb: AutomationVerb) => Promise<unknown>, a: Pick<AutomationView, "id" | "activeRunTaskId" | "status">) =>
  runNowShared((id) => act(id, "run"), a, errorCopy);
