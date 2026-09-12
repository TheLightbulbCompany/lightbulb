"use client";

import { useEffect, useMemo } from "react";
import useSWR from "swr";
import { useApi } from "@/lib/api";
import { isPresent } from "@/lib/presence";

const HEARTBEAT_MS = 45_000;
const SNAPSHOT_MS = 60_000;
// Slack's bar is "used the app in the last 30 minutes"; ours is tighter because
// the box-park gate already treats 2 minutes as "at the tab", and a green dot
// that outlives the person by half an hour is a lie the team acts on.
const ACTIVE_WITHIN_MS = 10 * 60_000;

/**
 * Who in this workspace is online: the set of user ids with a recent presence
 * heartbeat. While `enabled`, this tab heartbeats every 45s as long as it is
 * visible and someone has touched it in the last 10 minutes, and polls the
 * snapshot every 60s (the backend ages a heartbeat out after 90s).
 */
export function usePresence(enabled: boolean, scope: string): ReadonlySet<string> {
  const api = useApi();
  const { data } = useSWR<{ online: string[] }>(
    enabled ? ["messaging-presence", scope] : null,
    () => api.get("/messaging/presence") as Promise<{ online: string[] }>,
    { refreshInterval: SNAPSHOT_MS, dedupingInterval: 15_000, revalidateOnFocus: true },
  );
  useEffect(() => {
    if (!enabled) return;
    const beat = () => {
      if (document.visibilityState !== "visible" || !isPresent(ACTIVE_WITHIN_MS)) return;
      // Deferred so a synchronous throw from the client (or a stub) can never
      // escape the effect and unmount the shell; a missed beat self-heals next tick.
      void Promise.resolve().then(() => api.post("/messaging/presence", {})).catch(() => {});
    };
    beat();
    const timer = window.setInterval(beat, HEARTBEAT_MS);
    document.addEventListener("visibilitychange", beat);
    return () => { window.clearInterval(timer); document.removeEventListener("visibilitychange", beat); };
  }, [enabled, api]);
  return useMemo(() => new Set(data?.online ?? []), [data]);
}
