"use client";

import { useCallback, useEffect, useMemo, useRef } from "react";
import { useAuth } from "@clerk/nextjs";
import useSWR, {
  useSWRConfig,
  type KeyedMutator,
  type MutatorCallback,
  type MutatorOptions,
  type SWRConfiguration,
  type SWRResponse,
} from "swr";
import { useApi, type ApiCallOptions } from "@/lib/api";

/**
 * Teams BFF cookie opt-ins, applied to every `/teams/*` call (decision D2 —
 * least surface). `credentials: "include"` makes the browser attach the
 * HttpOnly Paperclip session cookie (scoped to `.thelightbulb.company`) on the
 * cross-subdomain request to the api host; the Clerk bearer header still
 * identifies the user. `retryOn401` re-issues the request once on a 401 —
 * the backend reseeds a fresh cookie on that 401 (body
 * `{"detail":"session refreshed, retry"}`), so the retry succeeds. Only Teams
 * calls opt in; the rest of the app keeps the bearer-only behavior.
 */
const TEAMS_API_OPTS: ApiCallOptions = {
  credentials: "include",
  retryOn401: true,
};

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

function isTeamsPath(path: unknown): path is string {
  return typeof path === "string" && /^\/teams(?:\/|\?|$)/.test(path);
}

/**
 * Hook for the teams BFF surface (`/api/v1/teams/*`).
 *
 * Centralizes all Clerk-authenticated calls into the BFF so re-routing the
 * base URL is a one-line change. Panels in `src/components/teams/panels/*`
 * use this hook for both reads (SWR-cached) and writes (one-shot mutations).
 *
 * Usage:
 *   const { read, post, put, patch, del } = useTeamsApi();
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
  const { isLoaded, userId, orgId } = useAuth();
  const { mutate: globalMutate } = useSWRConfig();
  const scope = useMemo(
    () => isLoaded && userId ? [userId, orgId ?? null] as const : null,
    [isLoaded, userId, orgId],
  );
  const cacheKey = useCallback(
    (path: string) => scope && isTeamsPath(path) ? [path, ...scope] as const : null,
    [scope],
  );
  const isCurrentScopeKey = useCallback((key: unknown) => (
    scope !== null && Array.isArray(key) && key.length === 3 &&
    isTeamsPath(key[0]) && key[1] === scope[0] && key[2] === scope[1]
  ), [scope]);
  const isCurrentScopeOrNonTeamsKey = useCallback((key: unknown) => (
    key !== undefined && (!isTeamsPath(Array.isArray(key) ? key[0] : key) || isCurrentScopeKey(key))
  ), [isCurrentScopeKey]);
  const mutate = useCallback(<Data = unknown>(
    path: string | ((path: string) => boolean),
    ...args: [data?: Data | Promise<Data> | MutatorCallback<Data>, opts?: boolean | MutatorOptions<Data>]
  ) => {
    if (typeof path === "string") return globalMutate(cacheKey(path), ...args);
    return globalMutate((key) => isCurrentScopeKey(key) && path((key as [string])[0]), ...args);
  }, [globalMutate, cacheKey, isCurrentScopeKey]);

  // Named `useRead` (use-prefix) so the react-hooks/rules-of-hooks lint accepts
  // the internal `useSWR` call. Exposed on the returned object as `read` for
  // ergonomic panel usage. Panels MUST call `read(...)` unconditionally at the
  // top of their component body (standard SWR rules of hooks apply).
  //
  // A `null` path is the canonical SWR "conditional fetching" signal — the
  // call still happens unconditionally (rules of hooks), but SWR skips the
  // request until the key flips from null to a real path. Consumers gate a
  // read on readiness by passing null until it should run.
  function useRead<T = unknown>(path: string | null, swrOpts?: SWRConfiguration<T>): SWRResponse<T> {
    const readScope = scope;
    const scopedKey = cacheKey;
    const mutateCache = globalMutate;
    const key = useMemo(() => path === null ? null : scopedKey(`/teams${path}`), [path, scopedKey]);
    // Diagnostic: trace every consumer call so we can correlate "Something went wrong"
    // overlays with the panel/hook that triggered them. Cheap (~one line per panel
    // mount), client-only (NEXT_PUBLIC_TEAMS_DEBUG gates it off in prod).
    if (
      path !== null &&
      typeof window !== "undefined" &&
      process.env.NEXT_PUBLIC_TEAMS_DEBUG === "1"
    ) {
      console.debug(`[useTeamsApi.read] ${path}`);
    }
    const dataScope = useRef<typeof readScope>(null);
    const response = useSWR<T>(
      key,
      async () => {
        const body = (await api.get(`/teams${path}`, TEAMS_API_OPTS)) as T | { detail?: string };
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
      // Teams reads are the most expensive calls in the app (BFF-proxied,
      // cookie + bearer). Panels unmount/remount on every shell navigation,
      // so a mount-level dedup window keeps a reopen from re-hitting the BFF
      // when the cached read is seconds old — TeamsEventsProvider's
      // event-driven mutations keep the data live in between. Callers can
      // override per read.
      {
        dedupingInterval: 15000,
        ...swrOpts,
        keepPreviousData: !!key && readScope === dataScope.current && swrOpts?.keepPreviousData,
      },
    );
    const data = response.data;
    useEffect(() => {
      if (data !== undefined) dataScope.current = readScope;
    }, [data, readScope]);
    const boundMutate = useMemo(
      () => mutateCache.bind(null, key) as KeyedMutator<T>,
      [mutateCache, key],
    );
    return { ...response, mutate: boundMutate };
  }

  async function post<T = unknown>(path: string, body: unknown): Promise<T> {
    return (await api.post(`/teams${path}`, body, TEAMS_API_OPTS)) as T;
  }

  // The BFF defines `@router.patch(...)` handlers on settings/routines/
  // goals/projects/issues/agents — routing this through PUT (the prior
  // implementation) silently 405'd because no PUT handlers exist. Use
  // `api.patch` so the actual HTTP method matches what the backend
  // accepts.
  async function patch<T = unknown>(path: string, body: unknown): Promise<T> {
    return (await api.patch(`/teams${path}`, body, TEAMS_API_OPTS)) as T;
  }

  async function put<T = unknown>(path: string, body: unknown): Promise<T> {
    return (await api.put(`/teams${path}`, body, TEAMS_API_OPTS)) as T;
  }

  async function del<T = unknown>(path: string): Promise<T> {
    return (await api.del(`/teams${path}`, TEAMS_API_OPTS)) as T;
  }

  // Multipart upload for Teams BFF routes that take a file (e.g. issue
  // attachments). The caller builds the FormData so it can name the field
  // exactly as the route expects ("file" for attachments).
  async function upload<T = unknown>(path: string, formData: FormData): Promise<T> {
    return (await api.upload(`/teams${path}`, formData, TEAMS_API_OPTS)) as T;
  }

  // Authenticated binary download (attachment content). Returns a Blob the
  // caller turns into an object URL — a bare <a href> can't carry the Clerk
  // bearer header, so the bytes are fetched here first.
  async function download(path: string): Promise<Blob> {
    return api.downloadBlob(`/teams${path}`, TEAMS_API_OPTS);
  }

  return {
    read: useRead,
    post,
    put,
    patch,
    del,
    upload,
    download,
    mutate,
    isCurrentScopeOrNonTeamsKey,
    workspaceId: orgId,
  };
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
 * Implementation: hits ``/teams/dashboard`` (cheapest existing endpoint)
 * and inspects the response/error.
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
    // The 202-provisioning poll above revalidates every 3s; the read
    // layer's mount-level dedup window would swallow those retries and
    // stall the overlay, so restore SWR's default here.
    dedupingInterval: 2000,
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
