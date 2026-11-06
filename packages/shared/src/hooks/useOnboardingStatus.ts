/**
 * `useOnboardingStatus` — the ONE client hook over `GET /container/status`
 * (spec PR-9, CLIENT-HOOK): a single SWR key fetched through ONE fetcher by
 * every web and mobile consumer.
 *
 * NO POLLING. `/container/status` is a cold read (Phase 3 slice 4): nothing
 * about it drives provisioning, so there is nothing to re-poll for. It is
 * fetched on mount, on visible/foreground (the injected `VisibilityAdapter`),
 * on reconnect (SWR's `revalidateOnReconnect`), on an explicit `refresh()`,
 * and — the live path — when a `runtime.resource.invalidated{resource:
 * "onboarding"}` event mutates the key (web `LiveEventsProvider`, mobile
 * `refreshOnboardingStatus`). Every provision state-machine status change
 * publishes that event. AppSync can drop one, which is what the foreground/
 * reconnect refetch covers; there is deliberately no timer.
 *
 * The fetcher is timeout-bounded (AbortController, `FETCH_TIMEOUT_MS`) and
 * THROWS on a network failure, the abort or an unrecognized status, so SWR
 * keeps the last good `data`, reports `networkError`, and retries with its
 * default backoff. Only 200/202/402/404 — real server states — resolve.
 *
 * Reads the CLEAN `StatusContract` fields only via `deriveUIPhase`. INV-1:
 * this hook is a PURE READER of the composer's contract; it never re-derives
 * readiness itself.
 */
import { useCallback, useEffect, useMemo, useRef } from "react";
import useSWR, { mutate } from "swr";

import { fetchJsonWithStatus, type ApiClientConfig } from "../api/client";
import { ApiError } from "../api/error";
import {
  classifyContainerStatus,
  publishStatusLoaded,
  setStatusScope,
  type ClassifiedStatus,
} from "./containerStatus";
import { deriveUIPhase, type DerivedUIPhase, type StatusContract } from "./statusContract";

/** AbortController timeout — a hung fetcher must settle (see module doc). */
const FETCH_TIMEOUT_MS = 10_000;

/** Platform seam for visibility/foreground state — web wraps
 * `document.visibilityState`, mobile wraps React Native's `AppState`. The
 * shared package compiles with no DOM lib, so this MUST be injected. */
export interface VisibilityAdapter {
  isVisible: () => boolean;
  /** Subscribe to visibility transitions; returns an unsubscribe function. */
  subscribe: (onChange: () => void) => () => void;
}

export interface UseOnboardingStatusOptions {
  /** Auth + base URL for the underlying fetch. */
  api: ApiClientConfig;
  /** False disables fetching entirely (e.g. signed out) — nulls the SWR key. */
  enabled: boolean;
  /** SWR key / endpoint path. Defaults to "/container/status". */
  swrKey?: string;
  /** Optional account/workspace cache scope. Never appended to the request URL. */
  scope?: string;
  /** Omit to never gate/pause — the hook then always treats itself as visible. */
  visibility?: VisibilityAdapter;
}

export interface UseOnboardingStatusResult {
  /** Raw status-code + payload-shape classification. Null before the first
   * settled fetch (loading) or while disabled. */
  classified: ClassifiedStatus | null;
  /** `deriveUIPhase` output when `classified.kind === "container"`; null
   * otherwise (202/402/404/unrecognized/not-yet-loaded all have no contract
   * body to derive from). */
  derived: DerivedUIPhase | null;
  /** The current fetch FAILED: a transport failure (network error, or the
   * AbortController timeout firing) or a non-2xx HTTP response that
   * classifies as `unrecognized` (e.g. a 5xx). Never set for a 200/202/402/
   * 404, which are valid server states the caller reads via `classified`.
   * Set INDEPENDENTLY of `classified`: SWR keeps the last good `data` while
   * `error` reports the failure, so a caller that wants to surface transient
   * trouble reads this, not a null status. */
  networkError: Error | undefined;
  isLoading: boolean;
  /** Forces an immediate revalidation. */
  refresh: () => void;
}

function derivePhase(classified: ClassifiedStatus | null): DerivedUIPhase | null {
  if (!classified || classified.kind !== "container") return null;
  // Defensive: an empty/malformed 200 body parses to `null` (see
  // fetchJsonWithStatus's catch-and-null-out on a JSON parse failure) —
  // treat it as "no contract to derive from" rather than crashing.
  const body = classified.body;
  if (!body || typeof body !== "object") return null;
  return deriveUIPhase(body as unknown as StatusContract);
}

export function useOnboardingStatus(
  options: UseOnboardingStatusOptions,
): UseOnboardingStatusResult {
  const { api, enabled, swrKey = "/container/status", scope, visibility } = options;

  // One string naming the workspace this read is for. The runtime-host store must go unknown
  // the moment the workspace changes — see `setStatusScope`.
  const statusScope = enabled ? (scope === undefined ? swrKey : `${swrKey}|${scope}`) : null;
  // Render-phase, no notify: the host must stop answering for the OLD workspace in the same
  // pass that the new key drops the old body, not one commit later.
  setStatusScope(statusScope);

  // Resolves only a REAL server state (200/202/402/404). A network failure, the timeout abort
  // or an unrecognized status THROWS, so SWR keeps the last good `data`, reports `error`, and
  // retries with its own backoff — a failed first fetch never wedges the UI.
  const fetcher = useCallback(async (): Promise<ClassifiedStatus> => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
    let classified: ClassifiedStatus;
    try {
      const { status, body } = await fetchJsonWithStatus(api, swrKey, controller.signal);
      classified = classifyContainerStatus({ status, body });
    } catch (err) {
      if (!(err instanceof ApiError)) {
        throw new Error(err instanceof Error ? err.message : "Request failed");
      }
      classified = classifyContainerStatus({ status: err.status, body: err.body });
    } finally {
      clearTimeout(timer);
    }
    if (classified.kind === "unrecognized") {
      // The status rides along (mobile routes a 409 `workspace_required` to its picker);
      // the message is unchanged.
      throw Object.assign(new Error(`Unexpected ${swrKey} status: ${classified.status}`), {
        status: classified.status,
      });
    }
    return classified;
  }, [api, swrKey]);

  const key = useMemo(
    () => enabled ? (scope === undefined ? swrKey : [swrKey, scope]) : null,
    [enabled, swrKey, scope],
  );

  const { data, error, mutate, isLoading } = useSWR<ClassifiedStatus, Error>(key, fetcher, {
    // Visibility resume is handled explicitly below (uniformly across web +
    // mobile via the injected adapter) instead of SWR's own DOM-coupled
    // revalidateOnFocus, which mobile has no equivalent of. No refreshInterval:
    // the status is refetched on events, never on a timer (see module doc).
    revalidateOnFocus: false,
    keepPreviousData: false,
  });

  const refresh = useCallback(() => {
    void mutate();
  }, [mutate]);

  // Resume-on-visible: an immediate revalidation the instant the tab/app
  // transitions to visible — the safety net for a live event AppSync dropped
  // while the tab/app was in the background.
  useEffect(() => {
    if (!key || !visibility) return;
    return visibility.subscribe(() => {
      if (visibility.isVisible()) void mutate();
    });
  }, [key, visibility, mutate]);

  const classified = key ? data ?? null : null;

  // This is the ONE reader of `/container/status`, so it is the one place that can hand the
  // runtime capability signal to surfaces that never see the body (M6 task 10, row 0). In an
  // effect, not during render — `publishStatusLoaded` notifies subscribers synchronously. It
  // carries `statusScope`, so a body that lands after a workspace switch can never be read as
  // the new workspace's answer.
  const body = classified?.kind === "container" ? classified.body : undefined;
  const statusLoaded = Boolean(body && typeof body === "object");
  useEffect(() => {
    if (statusScope && statusLoaded) publishStatusLoaded(statusScope);
  }, [statusScope, statusLoaded]);

  return {
    classified,
    derived: derivePhase(classified),
    networkError: key ? error : undefined,
    isLoading,
    refresh,
  };
}

/** Refetch the UNSCOPED status key now (mobile's). A ``runtime.resource.invalidated``
 *  nudge calls it — the event is how the stepper advances (there is no poll). */
export function refreshOnboardingStatus(swrKey = "/container/status"): void {
  void mutate(swrKey);
}

/** Drop every cached SWR read and refetch what is mounted. For a client-side workspace
 *  switch: runtime RPC keys and mobile's status key carry no workspace, so without this
 *  workspace B would render A's cached answers until each revalidated. */
export function resetSwrCache(): void {
  void mutate(() => true, undefined);
}

/** Drop every cached SWR read when the signed-in person changes (sign-out, or another account on this app). Most keys
 *  carry no user (a task, a room's detail, `/billing/account`), and each holds what one viewer may see: without this
 *  the next person would render the last one's answers until each revalidated. Nothing to drop on the first sign-in. */
export function useResetSwrOnUserChange(userId: string | null | undefined): void {
  const last = useRef(userId);
  useEffect(() => {
    if (last.current && last.current !== userId) void mutate(() => true, undefined, { revalidate: false });
    last.current = userId;
  }, [userId]);
}
