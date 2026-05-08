"use client";

import { useCallback } from "react";
import useSWR from "swr";
import { useAuth } from "@clerk/nextjs";
import { ApiError, BACKEND_URL } from "@/lib/api";

/**
 * Cold-start phase from `routers/container.py::_resolve_cold_start_phase`.
 * - `provisioning`: ECS task hasn't reached RUNNING yet (image pull, ENI
 *   attach, container init). Today this can be 60–120 s on a fresh service.
 * - `starting`: ECS task is RUNNING but the backend's gateway pool hasn't
 *   completed the OpenClaw 4.5 signed-device handshake + health RPC. This
 *   is where the long wait lives — gateway boot (≈30 s), then sidecars
 *   (channels + qmd memory init) which can run several minutes.
 * - `ready`: pool has a live, healthy connection to openclaw — chat works.
 */
export type ColdStartPhase = "provisioning" | "starting" | "ready";

export interface ContainerStatus {
  service_name: string;
  status: string;
  substatus: string | null;
  created_at: string | null;
  updated_at: string | null;
  region: string;
  last_error: string | null;
  last_error_at: string | null;
  /** Cold-start phase. Optional for backwards compat with old backends. */
  phase?: ColdStartPhase;
}

interface UseContainerStatusOptions {
  /** Polling interval in ms. 0 = no polling. Default: 0 */
  refreshInterval?: number;
  /** Whether to actively fetch. Default: true */
  enabled?: boolean;
}

export function useContainerStatus(options: UseContainerStatusOptions = {}) {
  const { refreshInterval = 0, enabled = true } = options;
  const { isSignedIn, getToken } = useAuth();

  // 402: a provision gate is up (handled by useProvisioningState in the
  // stepper). 202: org member's admin-owned workspace hasn't been
  // provisioned yet (also handled by useProvisioningState). For non-stepper
  // consumers (e.g. OverviewPanel, HealthIndicator), both states mean "no
  // container info to render" — return null instead of letting an error
  // propagate. After backend Task 8, /container/status no longer returns
  // 404 for the missing-container case (owners get 200/provisioning,
  // members get 202/team-pending), so a real 404 here would be a genuine
  // bug worth surfacing.
  //
  // Raw fetch (not `useApi().get`) because 202 is a 2xx response: the
  // standard `useApi.get` would collapse it into a body-only return,
  // hiding the status code we need to distinguish "owner already
  // provisioning" (200) from "team workspace pending" (202).
  const fetcher = useCallback(
    async (url: string): Promise<ContainerStatus | null> => {
      const token = await getToken();
      if (!token) throw new Error("No authentication token available");
      const response = await fetch(`${BACKEND_URL}${url}`, {
        method: "GET",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
      });
      if (response.status === 202 || response.status === 402) {
        return null;
      }
      const ct = response.headers.get("Content-Type") ?? "";
      let body: unknown = null;
      if (ct.includes("application/json")) {
        try {
          body = await response.json();
        } catch {
          // empty/malformed JSON — leave body as null
        }
      }
      if (response.ok) {
        return body as ContainerStatus;
      }
      throw new ApiError(response.status, body);
    },
    [getToken],
  );

  const { data, error, isLoading, mutate } = useSWR<ContainerStatus | null>(
    isSignedIn && enabled ? "/container/status" : null,
    fetcher,
    {
      // Revalidate when the tab regains focus. Without this, the chat
      // page can show a stale "running" container minutes after the row
      // is gone — observed when a container is deleted server-side
      // (admin tooling, scale-to-zero, AWS console cleanup) while the
      // tab was in the background.
      revalidateOnFocus: true,
      dedupingInterval: Math.min(refreshInterval || 30000, 30000),
      refreshInterval: refreshInterval || 0,
    },
  );

  const refresh = useCallback(() => mutate(), [mutate]);

  return {
    container: data,
    isLoading,
    error: error as Error | undefined,
    refresh,
  };
}
