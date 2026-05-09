"use client";

import useSWR, { type SWRConfiguration, type SWRResponse } from "swr";
import { useApi } from "@/lib/api";

/**
 * The BFF emits ``HTTPException(status_code=202, detail="team workspace
 * provisioning")`` when ``_ctx`` lazy-provisions a missing workspace. fetch()
 * treats any 2xx as success and surfaces the body verbatim, so panels would
 * otherwise see ``{detail}`` envelopes and render misleading empty/undefined
 * states. Detect the envelope at the read layer and convert it into a
 * thrown sentinel error so SWR's error path is uniformly hit on every
 * panel — TeamsLayout then handles the polling/UI globally.
 */
const PROVISIONING_DETAIL = "team workspace provisioning";

export interface TeamsApiError extends Error {
  status: number;
  detail?: string;
}

/**
 * Hook for the teams BFF surface (`/api/v1/teams/*`).
 *
 * Centralizes all Clerk-authenticated calls into the BFF so re-routing the
 * base URL is a one-line change. Panels in `src/components/teams/panels/*`
 * use this hook for both reads (SWR-cached) and writes (one-shot mutations).
 *
 * Usage:
 *   const { read, post, patch, del } = useTeamsApi();
 *   const { data, isLoading, error, mutate } = read<AgentList>("/agents");
 *   await post("/agents", { name, role });
 *
 * Note on rules-of-hooks: `read` calls `useSWR` internally, so it is a hook
 * itself. Call it unconditionally at the top of a component (the standard SWR
 * usage pattern). Do NOT wrap a `read(...)` call in a conditional.
 *
 * The SWR cache key is `/teams${path}`, which guarantees uniqueness per path
 * so different panels never share state.
 */
export function useTeamsApi() {
  const api = useApi();

  // Named `useRead` (use-prefix) so the react-hooks/rules-of-hooks lint accepts
  // the internal `useSWR` call. Exposed on the returned object as `read` for
  // ergonomic panel usage. Panels MUST call `read(...)` unconditionally at the
  // top of their component body (standard SWR rules of hooks apply).
  function useRead<T = unknown>(path: string, swrOpts?: SWRConfiguration<T>): SWRResponse<T> {
    // Diagnostic: trace every consumer call so we can correlate "Something went wrong"
    // overlays with the panel/hook that triggered them. Cheap (~one line per panel
    // mount), client-only (NEXT_PUBLIC_TEAMS_DEBUG gates it off in prod).
    if (
      typeof window !== "undefined" &&
      process.env.NEXT_PUBLIC_TEAMS_DEBUG === "1"
    ) {
      console.debug(`[useTeamsApi.read] ${path}`);
    }
    return useSWR<T>(
      `/teams${path}`,
      async () => {
        const body = (await api.get(`/teams${path}`)) as T | { detail?: string };
        if (
          body &&
          typeof body === "object" &&
          (body as { detail?: string }).detail === PROVISIONING_DETAIL
        ) {
          const err = new Error(PROVISIONING_DETAIL) as TeamsApiError;
          err.status = 202;
          err.detail = PROVISIONING_DETAIL;
          throw err;
        }
        return body as T;
      },
      swrOpts,
    );
  }

  async function post<T = unknown>(path: string, body: unknown): Promise<T> {
    return (await api.post(`/teams${path}`, body)) as T;
  }

  // The BFF defines `@router.patch(...)` handlers on settings/routines/
  // goals/projects/issues/agents — routing this through PUT (the prior
  // implementation) silently 405'd because no PUT handlers exist. Use
  // `api.patch` so the actual HTTP method matches what the backend
  // accepts.
  async function patch<T = unknown>(path: string, body: unknown): Promise<T> {
    return (await api.patch(`/teams${path}`, body)) as T;
  }

  async function del<T = unknown>(path: string): Promise<T> {
    return (await api.del(`/teams${path}`)) as T;
  }

  return { read: useRead, post, patch, del };
}

export type TeamsWorkspaceStatus =
  | { kind: "loading" }
  | { kind: "ready" }
  | { kind: "provisioning" }
  | { kind: "subscribe_required" }
  | { kind: "error"; error: TeamsApiError | Error };

/**
 * Single source of truth for "is the user's Teams workspace ready?".
 *
 * Consumed by ``TeamsLayout`` so the entire ``/teams/*`` tree shares one
 * provisioning/subscribe overlay rather than each panel rolling its own
 * detection. Drives an SWR auto-poll while in the provisioning state so
 * the layout flips to ``ready`` as soon as the BFF returns 200.
 *
 * Implementation: hits ``/teams/dashboard`` (cheapest existing endpoint;
 * SWR cache shares the request with ``DashboardPanel`` when the user is
 * actually on /teams/dashboard) and inspects the response/error.
 */
export function useTeamsWorkspaceStatus(): TeamsWorkspaceStatus {
  const { read } = useTeamsApi();
  // Auto-retry only on the 202 "provisioning" sentinel. Other errors
  // (402 subscribe-required, 5xx, etc.) stop retrying so the layout
  // can render its terminal state. ``errorRetryCount`` caps the
  // polling at ~90s before giving up — provision_org realistically
  // completes in 10-30s, so 30 retries × 3s is generous headroom.
  const { data, error, isLoading } = read<unknown>("/dashboard", {
    errorRetryInterval: 3000,
    errorRetryCount: 30,
    shouldRetryOnError: (err: unknown) =>
      (err as TeamsApiError | undefined)?.status === 202,
  });
  if (error) {
    const e = error as TeamsApiError;
    if (e.status === 202) return { kind: "provisioning" };
    if (e.status === 402) return { kind: "subscribe_required" };
    return { kind: "error", error: e };
  }
  if (isLoading) return { kind: "loading" };
  if (data) return { kind: "ready" };
  return { kind: "loading" };
}
