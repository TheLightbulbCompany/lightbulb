"use client";

import useSWR from "swr";

import { useApi } from "@/lib/api";
import { notifyApiError } from "@/lib/notify";

export type AppRow = {
  app_id: string;
  name: string;
  room_id: string;
  room_name?: string;
  state: string;
  open_to: "room" | "workspace" | "owner" | string;
  budget_usd: number;
  preview_url: string | null;
  live_url: string | null;
  live_version: number | null;
  last_opened_at: string | null;
  spend_month_cents: number;
  created_by_name?: string;
  agent_name?: string;
  can_open: boolean;
  resources?: { provider: string; service: string; state: string; resource_id?: string; environment?: string }[];
};

type AppsResponse = { apps: AppRow[] };

/** The workspace's apps (member routes). Refreshes every 15 s while any app is building. */
export function useApps() {
  const api = useApi();
  const { data, error, isLoading, mutate } = useSWR<AppsResponse>(
    "/apps",
    (path: string) => api.get(path) as Promise<AppsResponse>,
    { refreshInterval: (latest) => (latest?.apps.some((a) => a.state === "building") ? 15_000 : 0) },
  );

  /** Returns the URL to frame. Errors propagate: the caller shows the backend's refusal. */
  const open = async (appId: string, target: "live" | "preview" = "live"): Promise<string> => {
    const suffix = target === "preview" ? "?target=preview" : "";
    const res = await api.post(`/apps/${encodeURIComponent(appId)}/open${suffix}`, {}) as { url: string };
    return res.url;
  };

  const update = async (appId: string, patch: { open_to?: AppRow["open_to"]; budget_usd?: number }): Promise<AppRow> => {
    try {
      const row = await api.patch(`/apps/${encodeURIComponent(appId)}`, patch) as AppRow;
      await mutate();
      return row;
    } catch (err) {
      notifyApiError(err, "The app could not be updated. Try again.");
      throw err;
    }
  };

  return {
    apps: data?.apps ?? [],
    loading: isLoading,
    error: (error as Error | undefined) ?? null,
    refresh: () => { void mutate(); },
    open,
    update,
  };
}
