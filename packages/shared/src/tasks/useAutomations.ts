// SWR hooks over lane A's automation routes (#4754), keyed by the workspace like the task lists.
// Both apps read through these; each passes its signed-in API client and workspace scope.

import useSWR from "swr";

import type { ApiMethods } from "../api/client";
import type { AutomationView, TaskSummary } from "./types";

const noRetry = { shouldRetryOnError: false } as const;

/** The signed-in API client and the workspace the cache keys carry. */
export interface AutomationSource { api: ApiMethods; cacheScope: string }

export const automationsKey = (cacheScope: string) => ["automations", cacheScope] as const;
export const automationKey = (cacheScope: string, id: string) => ["automation", cacheScope, id] as const;
export const automationRunsKey = (cacheScope: string, id: string) => ["automation-runs", cacheScope, id] as const;

export function useAutomations({ api, cacheScope }: AutomationSource, enabled = true) {
  return useSWR<{ automations: AutomationView[] }>(enabled ? automationsKey(cacheScope) : null,
    () => api.get("/automations") as Promise<{ automations: AutomationView[] }>, noRetry);
}

export function useAutomation({ api, cacheScope }: AutomationSource, id: string | null) {
  return useSWR<AutomationView>(id ? automationKey(cacheScope, id) : null,
    () => api.get(`/automations/${encodeURIComponent(id!)}`) as Promise<AutomationView>, noRetry);
}

export function useAutomationRuns({ api, cacheScope }: AutomationSource, id: string | null) {
  return useSWR<{ runs: TaskSummary[]; next: string | null }>(id ? automationRunsKey(cacheScope, id) : null,
    () => api.get(`/automations/${encodeURIComponent(id!)}/runs`) as Promise<{ runs: TaskSummary[]; next: string | null }>, noRetry);
}
