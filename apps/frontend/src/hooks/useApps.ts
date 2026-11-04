"use client";

import { useAuth } from "@clerk/nextjs";
import { useCallback, useEffect } from "react";
import useSWR from "swr";

import { useLiveEvents } from "@/hooks/useLiveEvents";
import { useApi } from "@/lib/api";
import { notifyApiError } from "@/lib/notify";

export type AppRow = {
  app_id: string;
  name: string;
  room_id: string | null; // null: the app came from no room (CLI, or asked for without one)
  room_name?: string;
  conversation_id?: string | null; // its own conversation with its builder, once opened (POST /apps/{id}/conversation)
  state: "requested" | "awaiting_approval" | "provisioning" | "building" | "ready" | "live" | "failed" | "destroyed";
  open_to: "me" | "room" | "workspace" | "invited" | "public";
  budget_usd: number;
  preview_url: string | null;
  live_url: string | null;
  live_version: number | null;
  last_opened_at: string | null;
  updated_at?: string | null; // the last change to the app: a build step, a publish, a setting
  spend_month_cents: number | null; // null: the backend does not know yet
  requested_by?: string; // the person who asked for the app
  display_name?: string; // person-facing name; Canvas title-cases the slug when absent
  created_by_name?: string;
  is_creator?: boolean; // the viewer made it (or asked an agent for it)
  agent_name?: string;
  can_open: boolean;
  stuck?: { at: string; step: string } | null;
  setup_blocker?: { message: string } | null; // only a person can fix it, e.g. no Stripe services account linked
  agent_working?: boolean; // an agent is still improving the preview: it's a first version
  can_publish?: boolean; // mock (R17): plan 1 computes it
  error?: { message: string } | null; // the last build/publish failure, when the backend reports one
  publish_failed?: boolean; // a live app's state stays "live" through a failed publish
  publishing?: boolean; // a publish is in flight, this person's or an agent's: putting back cancels it
  preview_newer?: boolean; // the ready preview isn't what is live, so Publish has something to do
  previous_version?: { n: number; at: string; rebuild?: boolean } | null; // what "Put back the previous version" goes back to; rebuild: it takes a few minutes
  resources?: { provider: string; service: string; state: string; resource_id?: string; environment?: string }[];
};

const PENDING: AppRow["state"][] = ["requested", "awaiting_approval", "provisioning", "building"];

type AppsResponse = { apps: AppRow[] };

/** The workspace's apps (member routes). Refreshes every 15 s while any app is still being set up. */
export function useApps() {
  const api = useApi();
  const { isLoaded, userId, orgId } = useAuth();
  const { data, error, isLoading, mutate } = useSWR<AppsResponse>(
    isLoaded && userId ? ["/apps", `${userId}:${orgId ?? userId}`] : null,
    ([path]: [string, string]) => api.get(path) as Promise<AppsResponse>,
    { refreshInterval: (latest) => (latest?.apps.some((a) => PENDING.includes(a.state)) ? 15_000 : 0) },
  );

  // An app changed server-side: refetch now. The poll above stays as a backstop.
  const live = useLiveEvents();
  useEffect(
    () => live.onEvent((event) => {
      if (event.type === "runtime.resource.invalidated" && event.payload.resource === "apps") void mutate();
    }),
    [live, mutate],
  );

  /** Returns the URL to frame. Errors propagate: the caller shows the backend's refusal. */
  const open = async (appId: string, target: "live" | "preview" = "live"): Promise<string> => {
    const suffix = target === "preview" ? "?target=preview" : "";
    const res = await api.post(`/apps/${encodeURIComponent(appId)}/open${suffix}`, {}) as { url: string };
    return res.url;
  };

  /** The app's conversation with its builder, made on the first open, with the person seated. Stable: SessionChat re-opens when it changes. */
  const openConversation = useCallback(async (appId: string): Promise<{ roomId: string }> => {
    const res = await api.post(`/apps/${encodeURIComponent(appId)}/conversation`, {}) as { room_id: string };
    void mutate(); // the row now has its conversation_id: copy about where the builder posts follows it
    return { roomId: res.room_id };
  }, [api, mutate]);

  /** Canvas's "What should we build?": the app gets its own conversation and a builder. Errors propagate. */
  const start = async (request: string): Promise<{ app_id: string }> => {
    const res = await api.post("/apps/start", { request }) as { app_id: string };
    void mutate();
    return res;
  };

  /** Starts a publish and returns at once; the caller re-reads the list for progress. Errors propagate: the caller shows the backend's message. */
  const publish = async (appId: string): Promise<{ state?: string; error?: { message: string } | null }> => {
    const res = await api.post(`/apps/${encodeURIComponent(appId)}/publish`, {}) as { state?: string; error?: { message: string } | null };
    await mutate(); // the list must show the new state before the caller trusts a "failed" row
    return res;
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

  /** Who holds a live invite. Errors propagate: Settings offers Try again. */
  const listInvites = async (appId: string): Promise<string[]> => {
    const res = await api.get(`/apps/${encodeURIComponent(appId)}/invites`) as { emails: string[] };
    return res.emails;
  };

  const revokeInvite = async (appId: string, email: string): Promise<void> => {
    try {
      await api.del(`/apps/${encodeURIComponent(appId)}/invites`, undefined, { email });
    } catch (err) {
      notifyApiError(err, "The invite could not be removed. Try again.");
      throw err;
    }
  };

  /** Posts the error in the app's conversation as the person (an older app's room until its conversation is opened). Errors propagate: the caller shows them inline. */
  const askAgent = async (appId: string, error?: string): Promise<void> => {
    await api.post(`/apps/${encodeURIComponent(appId)}/ask-agent`, error ? { error: error.slice(0, 2000) } : {});
  };

  /** Puts the named version live again, or starts rebuilding it. Errors propagate: the caller shows them inline. */
  const rollback = async (appId: string, version: number): Promise<{ rebuilding?: boolean }> => {
    const res = await api.post(`/apps/${encodeURIComponent(appId)}/rollback`, { version }) as { rebuilding?: boolean };
    await mutate();
    return res;
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
      throw err; // the caller shows it inline
    }
  };

  return {
    apps: (data?.apps ?? []).filter((a) => a.state !== "destroyed"), // removed apps linger server-side
    loading: isLoading,
    error: (error as Error | undefined) ?? null,
    refresh: () => { void mutate(); },
    open,
    openConversation,
    start,
    publish,
    update,
    invite,
    listInvites,
    revokeInvite,
    askAgent,
    rollback,
    remove,
    databaseLink,
  };
}
