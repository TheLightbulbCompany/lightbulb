// SWR hooks over lane A's automation routes (#4754), keyed by the workspace like the task lists.
// Both apps read through these so neither imports swr itself.

import useSWR from "swr";

import type { ApiMethods } from "../api/client";
import type { AutomationView, TaskSummary } from "./types";

const noRetry = { shouldRetryOnError: false } as const;

export const automationsKey = (cacheScope: string) => ["automations", cacheScope] as const;
export const automationKey = (cacheScope: string, id: string) => ["automation", cacheScope, id] as const;
export const automationRunsKey = (cacheScope: string, id: string) => ["automation-runs", cacheScope, id] as const;

export function useAutomations(api: ApiMethods, cacheScope: string, enabled = true) {
  return useSWR<{ automations: AutomationView[] }>(enabled ? automationsKey(cacheScope) : null,
    () => api.get("/automations") as Promise<{ automations: AutomationView[] }>, noRetry);
}

export function useAutomation(api: ApiMethods, cacheScope: string, id: string | null) {
  return useSWR<AutomationView>(id ? automationKey(cacheScope, id) : null,
    () => api.get(`/automations/${encodeURIComponent(id!)}`) as Promise<AutomationView>, noRetry);
}

export function useAutomationRuns(api: ApiMethods, cacheScope: string, id: string | null) {
  return useSWR<{ runs: TaskSummary[]; next: string | null }>(id ? automationRunsKey(cacheScope, id) : null,
    () => api.get(`/automations/${encodeURIComponent(id!)}/runs`) as Promise<{ runs: TaskSummary[]; next: string | null }>, noRetry);
}
