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
 * REJECTION-SAFE: it never throws. Every outcome (success, HTTP error, network
 * failure, abort) resolves to a typed `Outcome`.
 *
 * That costs SWR's keep-last-good-on-error behavior (a resolved value
 * overwrites `data`; a thrown one would not). So `classified`/`derived` are
 * STICKY on the last outcome that represented a REAL SERVER STATE (200/202/
 * 402/404). Both failure routes — a rejected/aborted fetch AND a non-2xx that
 * classifies as `unrecognized` — report via `networkError` without blanking
 * the status out from under every consumer. Read that comment before changing
 * what the hook returns: dropping the stickiness silently unmounts whatever
 * onboarding UI is rendered on top of the status, taking any in-progress user
 * input with it.
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
  /** Computed fresh at each fetch ATTEMPT and appended as `?wake=0|1`. Omit
   * to never send the param (an old backend simply ignores it). */
  wake?: () => boolean;
  /** Client-minted signup_flow_id, appended as `?flow=`. Omit to never send
   * it (mobile does not run the web signup funnel). */
  flow?: () => string | undefined;
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
   * Set INDEPENDENTLY of `classified`: on a blip `classified` still holds
   * the last good state while this reports the failure, so a caller that
   * wants to surface transient trouble reads this, not a null status. */
  networkError: Error | undefined;
  isLoading: boolean;
  /** Forces an immediate revalidation. */
  refresh: () => void;
}

type Outcome =
  | { kind: "classified"; classified: ClassifiedStatus }
  | { kind: "network-error"; error: Error };

function derivePhaseFromOutcome(outcome: Outcome | undefined): DerivedUIPhase | null {
  if (!outcome || outcome.kind !== "classified" || outcome.classified.kind !== "container") {
    return null;
  }
  // Defensive: an empty/malformed 200 body parses to `null` (see
  // fetchJsonWithStatus's catch-and-null-out on a JSON parse failure) —
  // treat it as "no contract to derive from" rather than crashing.
  const body = outcome.classified.body;
  if (!body || typeof body !== "object") return null;
  return deriveUIPhase(body as unknown as StatusContract);
}

export function useOnboardingStatus(
  options: UseOnboardingStatusOptions,
): UseOnboardingStatusResult {
  const { api, enabled, swrKey = "/container/status", scope, wake, flow, visibility } = options;

  // One string naming the workspace this read is for. The runtime-host store must go unknown
  // the moment the workspace changes — see `setStatusScope`.
  const statusScope = enabled ? (scope === undefined ? swrKey : `${swrKey}|${scope}`) : null;
  // Render-phase, no notify: the host must stop answering for the OLD workspace in the same
  // pass that `lastGoodRef` below drops the old body, not one commit later.
  setStatusScope(statusScope);

  const fetcher = useCallback(async (): Promise<Outcome> => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
    try {
      const wakeFlag = wake ? (wake() ? "1" : "0") : undefined;
      const flowId = flow?.();
      const params = new URLSearchParams();
      if (wakeFlag !== undefined) params.set("wake", wakeFlag);
      if (flowId) params.set("flow", flowId);
      const query = params.toString();
      // Request parameters and cache scope are separate from the endpoint.
      const url = query ? `${swrKey}${swrKey.includes("?") ? "&" : "?"}${query}` : swrKey;
      const { status, body } = await fetchJsonWithStatus(api, url, controller.signal);
      return { kind: "classified", classified: classifyContainerStatus({ status, body }) };
    } catch (err) {
      if (err instanceof ApiError) {
        return {
          kind: "classified",
          classified: classifyContainerStatus({ status: err.status, body: err.body }),
        };
      }
      // Rejection-safe (see module doc): NEVER throw out of the fetcher.
      // Covers a real network failure and the AbortController firing
      // (DOMException "AbortError") — both resolve to a typed outcome
      // instead of an uncaught rejection, so SWR's promise chain always
      // takes its success path and keeps rescheduling.
      const message = err instanceof Error ? err.message : "Request failed";
      return { kind: "network-error", error: new Error(message) };
    } finally {
      clearTimeout(timer);
    }
  }, [api, swrKey, wake, flow]);

  const key = useMemo(
    () => enabled ? (scope === undefined ? swrKey : [swrKey, scope]) : null,
    [enabled, swrKey, scope],
  );
  const previousKey = useRef<typeof key>(null);
  const lastGoodRef = useRef<Outcome | undefined>(undefined);
  if (previousKey.current !== key) {
    previousKey.current = key;
    lastGoodRef.current = undefined;
  }

  const { data, mutate, isLoading } = useSWR<Outcome>(key, fetcher, {
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

  // Last outcome that actually CLASSIFIED. The fetcher above is
  // rejection-safe by design — it resolves failures instead of throwing, so
  // a failed fetch can never surface as an unhandled rejection (see module doc).
  // The cost of that is invisible until you look at SWR's semantics: a
  // RESOLVED value is DATA, so a failed fetch OVERWRITES the last good
  // response, where a thrown error would have sat alongside it in `error`
  // and left `data` intact. Net effect without this ref: one blip (a
  // transient 5xx, or the AbortController firing on a slow fetch)
  // blanks `classified` until the next fetch, every consumer reading it
  // falls back to a loading state, and any UI mounted on top — the first-run
  // funnel most visibly — UNMOUNTS, taking the user's in-progress typing
  // with it.
  //
  // So: keep the rejection-safety and add the
  // stickiness back here (it protects the state). `networkError` still
  // reports the blip, so callers that care can surface it.
  //
  // Deliberately NOT sticky across a null key: signing out (or otherwise
  // disabling the hook) must drop the status, not leave a stale container
  // on screen.
  // A "classified" outcome is NOT automatically a server state. Failure
  // reaches us by two different routes and both must be non-destructive:
  //   • a rejected/aborted fetch      -> `network-error`
  //   • a non-2xx HTTP response (5xx) -> `classified` / `unrecognized`,
  //     because the client THROWS ApiError, the fetcher catches it, and
  //     `classifyContainerStatus` files anything outside 200/202/402/404
  //     into its "can't represent this" bucket.
  // Only those four codes describe a real owner state worth remembering, so
  // `unrecognized` is treated as a failure here, exactly like a rejection.
  const isServerState =
    data?.kind === "classified" && data.classified.kind !== "unrecognized";

  if (!key) {
    lastGoodRef.current = undefined;
  } else if (isServerState) {
    lastGoodRef.current = data;
  }
  const lastGood = lastGoodRef.current;

  const classified = lastGood?.kind === "classified" ? lastGood.classified : null;

  // This is the ONE reader of `/container/status`, so it is the one place that can hand the
  // runtime capability signal to surfaces that never see the body (M6 task 10, row 0). In an
  // effect, not during render — `publishStatusLoaded` notifies subscribers synchronously. It
  // carries `statusScope`, so a body that lands after a workspace switch can never be read as
  // the new workspace's answer.
  // Defensive about the body exactly as `derivePhaseFromOutcome` is: an empty or malformed
  // 200 parses to `null` (fetchJsonWithStatus nulls out a JSON parse failure).
  const body = classified?.kind === "container" ? classified.body : undefined;
  const statusLoaded = Boolean(body && typeof body === "object");
  useEffect(() => {
    if (statusScope && statusLoaded) publishStatusLoaded(statusScope);
  }, [statusScope, statusLoaded]);
  // Both failure routes surface here, so a caller with NO last good to fall
  // back on still gets a real error rather than a silently stuck UI — the
  // contract `useProvisioningState` depends on for an unrecognized status.
  // Memoized on the outcome: a fresh Error identity every render would churn
  // any consumer that puts this in a dependency array.
  const networkError = useMemo(() => {
    if (data?.kind === "network-error") return data.error;
    if (data?.kind === "classified" && data.classified.kind === "unrecognized") {
      return new Error(`Unexpected ${swrKey} status: ${data.classified.status}`);
    }
    return undefined;
  }, [data, swrKey]);

  return {
    classified,
    derived: derivePhaseFromOutcome(lastGood),
    networkError,
    isLoading,
    refresh,
  };
}

/** Refetch the UNSCOPED status key now (mobile's). A ``runtime.resource.invalidated``
 *  nudge calls it — the event is how the stepper advances (there is no poll). */
export function refreshOnboardingStatus(swrKey = "/container/status"): void {
  void mutate(swrKey);
}
