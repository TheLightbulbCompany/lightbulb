"use client";

import { useAuth } from "@clerk/nextjs";
import useSWR from "swr";

import { useApi } from "@/lib/api";
import { notifyApiError } from "@/lib/notify";

export type AppRow = {
  app_id: string;
  name: string;
  room_id: string;
  room_name?: string;
  state: "awaiting_approval" | "provisioning" | "building" | "ready" | "failed" | "destroyed";
  open_to: "room" | "workspace" | "invited" | "public";
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
  const { isLoaded, userId, orgId } = useAuth();
  const { data, error, isLoading, mutate } = useSWR<AppsResponse>(
    isLoaded && userId ? ["/apps", `${userId}:${orgId ?? userId}`] : null,
    ([path]: [string, string]) => api.get(path) as Promise<AppsResponse>,
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

  const invite = async (appId: string, emails: string[]): Promise<void> => {
    try {
      await api.post(`/apps/${encodeURIComponent(appId)}/invites`, { emails });
    } catch (err) {
      notifyApiError(err, "The invites could not be sent. Try again.");
      throw err;
    }
  };

  const remove = async (appId: string, confirmName: string): Promise<void> => {
    try {
      await api.del(`/apps/${encodeURIComponent(appId)}`, undefined, { confirm_name: confirmName });
      await mutate();
    } catch (err) {
      notifyApiError(err, "The app could not be removed. Try again.");
      throw err;
    }
  };

  /** Console link for the app's live database (prod Neon, else the first Neon resource); null when it has none. */
  const databaseLink = async (app: AppRow): Promise<string | null> => {
    const neon = (app.resources ?? []).filter((r) => r.provider === "neon" && r.resource_id);
    const target = neon.find((r) => r.environment === "prod") ?? neon[0];
    if (!target) return null;
    try {
      const res = await api.post(`/company/provisioning/resources/neon/${encodeURIComponent(target.resource_id!)}/dashboard-link`, {}) as { url?: string | null };
      return res.url ?? null;
    } catch (err) {
      notifyApiError(err, "The database console could not be opened. Try again.");
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
    invite,
    remove,
    databaseLink,
  };
}
