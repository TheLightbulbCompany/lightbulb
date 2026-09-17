/**
 * `useOnboardingStatus` — the ONE client hook over `GET /container/status`
 * (spec PR-9, CLIENT-HOOK). Collapses the historical per-platform freshness
 * stack (web: 3s SWR poll + `revalidateOnFocus` + a 1s watchdog timer +
 * `useSystemHealth`'s own poll + an OverviewPanel focus opt-in + 3 manual
 * `mutate("/container/status")` call sites; mobile: its own hand-rolled
 * `useState` + raw `setInterval` + `AppState` loop) into a single SWR key
 * fetched through ONE fetcher, with ONE bounded backstop loop belt-and-
 * suspenders behind SWR's own scheduling. See
 * docs/superpowers/specs/2026-07-24-onboarding-architecture-design.md §3/§9
 * and docs/superpowers/research/2026-07-24-onboarding-current-state.md §7.
 *
 * Two independent fixes, per the research's root-cause finding that the
 * real kill vector for a throttled-but-visible tab is a HUNG FETCHER or a
 * REJECTED revalidate — not a dropped SWR tick:
 *
 *   1. **Root fix** — the fetcher below is timeout-bounded (AbortController,
 *      `FETCH_TIMEOUT_MS`) and REJECTION-SAFE: it never throws. Every
 *      outcome (success, HTTP error, network failure, abort) resolves to a
 *      typed `Outcome`, so SWR's internal revalidation promise always
 *      settles via its success path — the `.then(next)`-with-no-catch
 *      landmine documented in the research can't fire because there is
 *      nothing left to reject.
 *   2. **Belt-and-suspenders backstop** — a RAW `setInterval`, independent of
 *      SWR's self-rescheduling `refreshInterval` chain, that force-mutates
 *      whenever the last fetch ATTEMPT is older than the active cadence +
 *      slack. Because it's a fresh interval rather than a chain of promise-
 *      chained timeouts, it survives whatever mechanism killed the chain.
 *
 * Those two make the fetcher resolve errors as DATA, which costs SWR's
 * keep-last-good-on-error behavior (a resolved value overwrites `data`; a
 * thrown one would not). So a third property is restored explicitly at the
 * return site: `classified`/`derived` are STICKY on the last outcome that
 * represented a REAL SERVER STATE (200/202/402/404). Both failure routes —
 * a rejected/aborted fetch AND a non-2xx that classifies as `unrecognized`
 * — report via `networkError` without blanking the status out from under
 * every consumer. Read that comment before changing what the hook returns:
 * dropping the stickiness silently unmounts whatever onboarding UI is
 * rendered on top of the status, taking any in-progress user input with it.
 *
 * Cadence: 5s while unsettled and within the first 60s of the CURRENT
 * unsettled streak, 15s after that, 0 (off) once `deriveUIPhase` reports
 * `phase === "ready"`. Deliberately NOT off at "parked": a parked (wake-
 * gated trial) container needs the poll's own `wake` query param to drive
 * itself parked -> starting -> ready (see the `wake` option below) — this is
 * what lets CLIENT-HOOK delete the old wake/unpark/container_waking manual
 * `mutate()` trio: the next bounded-loop tick (<=15s away) picks up the
 * transition on its own.
 *
 * Visibility-gated: the backstop (and the resume-on-visible kick) pause
 * while `visibility.isVisible()` is false (genuinely hidden/backgrounded).
 * Chrome's occlusion throttling covers a VISIBLE-but-covered tab, which
 * never flips visibility — so the backstop keeps ticking through exactly
 * the scenario the research verified live (a throttled-but-visible tab
 * making ~1 request in 3+ minutes).
 *
 * Reads the CLEAN `StatusContract` fields only (`runtime_phase`,
 * `lifecycle`, `container_status`, `container_substatus`, `teams_state`,
 * `chat_available`, `onboarded`, `funnel`, `bootstrap`, `generations`) via
 * `deriveUIPhase` — never the legacy flat duplicates. INV-1: this hook is a
 * PURE READER of the composer's contract; it never re-derives readiness
 * itself.
 */
import { useCallback, useEffect, useMemo, useRef } from "react";
import useSWR from "swr";

import { fetchJsonWithStatus, type ApiClientConfig } from "../api/client";
import { ApiError } from "../api/error";
import {
  classifyContainerStatus,
  publishRuntimeHost,
  setRuntimeHostScope,
  type ClassifiedStatus,
} from "./containerStatus";
import { deriveUIPhase, type DerivedUIPhase, type StatusContract } from "./statusContract";

const FAST_POLL_MS = 5_000;
const SLOW_POLL_MS = 15_000;
const FAST_WINDOW_MS = 60_000;
/** AbortController timeout — the root fix for a hung fetcher (see module doc). */
const FETCH_TIMEOUT_MS = 10_000;
/** Raw backstop tick — independent of SWR's own scheduling. */
const BACKSTOP_TICK_MS = 5_000;
/** Grace before the backstop decides a fetch is overdue and force-mutates. */
const BACKSTOP_SLACK_MS = 3_000;

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
  /** Client-minted signup_flow_id, appended as `?flow=`. This poll is what
   * creates a new workspace's trial row, so it is the one request that
   * can hand the id to the server-side `trial_started`. Omit to never send
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
  /** The current poll FAILED: a transport failure (network error, or the
   * AbortController timeout firing) or a non-2xx HTTP response that
   * classifies as `unrecognized` (e.g. a 5xx). Never set for a 200/202/402/
   * 404, which are valid server states the caller reads via `classified`.
   * Set INDEPENDENTLY of `classified`: on a blip `classified` still holds
   * the last good state while this reports the failure, so a caller that
   * wants to surface transient trouble reads this, not a null status. */
  networkError: Error | undefined;
  isLoading: boolean;
  /** Forces an immediate revalidation and resets the fast-poll window. */
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

function isSettledReady(outcome: Outcome | undefined): boolean {
  return derivePhaseFromOutcome(outcome)?.phase === "ready";
}

function cadenceFor(ready: boolean, windowStartedAt: number, now: number): number {
  if (ready) return 0;
  return now - windowStartedAt < FAST_WINDOW_MS ? FAST_POLL_MS : SLOW_POLL_MS;
}

/** Last fetch attempt per SWR key, shared across every hook instance. */
const lastAttemptByKey = new Map<string, { current: number }>();

function useSharedAttemptClock(key: string | null): { current: number } {
  const fallback = useRef(0);
  if (key === null) return fallback;
  let clock = lastAttemptByKey.get(key);
  if (!clock) {
    clock = { current: 0 };
    lastAttemptByKey.set(key, clock);
  }
  return clock;
}

export function useOnboardingStatus(
  options: UseOnboardingStatusOptions,
): UseOnboardingStatusResult {
  const { api, enabled, swrKey = "/container/status", scope, wake, flow, visibility } = options;

  // Timestamp of the most recent fetch ATTEMPT (not completion) — read by
  // the backstop to decide whether the SWR chain looks stalled.
  //
  // Keyed by the SWR key, NOT held per hook instance. SWR dedupes the request
  // across every mount of the same key, so only the instance whose fetcher
  // actually ran would ever update a private ref — and `/chat` mounts this four
  // times (ProvisioningStepper, useChatReady, LaunchJoinModal, useSystemHealth).
  // The other three saw a ref frozen at 0, concluded the chain was stalled on
  // every tick, and called `mutate()` every 5 seconds forever.
  // One string naming the workspace this poll is for. Shared by the attempt clock (which is
  // per SWR key) and the runtime-host store (which must go unknown the moment the workspace
  // changes — see `setRuntimeHostScope`).
  const statusScope = enabled ? (scope === undefined ? swrKey : `${swrKey}|${scope}`) : null;
  const lastAttemptRef = useSharedAttemptClock(statusScope);
  // Render-phase, no notify: the host must stop answering for the OLD workspace in the same
  // pass that `lastGoodRef` below drops the old body, not one commit later.
  setRuntimeHostScope(statusScope);
  // Start of the CURRENT unsettled streak, for the 60s fast->slow cadence
  // step. Reset on mount, on a ready->not-ready regression, and on refresh().
  const windowStartRef = useRef<number>(Date.now());
  const prevReadyRef = useRef(false);

  const fetcher = useCallback(async (): Promise<Outcome> => {
    lastAttemptRef.current = Date.now();
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
    windowStartRef.current = Date.now();
    prevReadyRef.current = false;
    // `lastAttemptRef` is deliberately NOT reset here. It is shared per SWR key
    // now, and this branch fires on every fresh mount (each instance starts
    // with `previousKey.current === null`) — so zeroing it would let a
    // late-joining consumer stomp the clock of instances already polling that
    // key, and their next backstop tick would read the poll as overdue. It
    // bought nothing even when private: the deduped fetcher stamps a real
    // timestamp on the next actual attempt either way.
  }

  // STABLE identity, deliberately. SWR lists `refreshInterval` in its polling
  // effect's dependency array and the effect's cleanup CLEARS the pending
  // tick, so an inline closure (a new identity every render) tears the timer
  // down and re-arms a fresh `setTimeout(cadence)` on every render of every
  // consumer. A consumer that re-renders faster than the cadence therefore
  // resets the timer before it can ever fire — and the web stepper ticks an
  // elapsed counter once a SECOND for the entire cold start, i.e. exactly
  // while the readiness poll matters most. Measured: 3 polls per 30s (only
  // what the backstop below rescued) vs the 6 the 5s cadence declares. Reads
  // refs only, so it never needs to change identity.
  const refreshIntervalFn = useCallback(
    (latest?: Outcome) => cadenceFor(isSettledReady(latest), windowStartRef.current, Date.now()),
    [],
  );

  const { data, mutate, isLoading } = useSWR<Outcome>(key, fetcher, {
    // Visibility resume is handled explicitly below (uniformly across web +
    // mobile via the injected adapter) instead of SWR's own DOM-coupled
    // revalidateOnFocus, which mobile has no equivalent of.
    revalidateOnFocus: false,
    keepPreviousData: false,
    refreshInterval: refreshIntervalFn,
  });

  // Cadence reads the LIVE outcome, never the sticky last-good below: a blip
  // over a ready container must drop `ready` so polling resumes at the fast
  // cadence and discovers the recovery. Sourcing this from the sticky value
  // would hold cadence at 0 (off) and wedge the loop on the last good read.
  const ready = isSettledReady(data);

  // Rising edge INTO "not ready" (including first mount) restarts the fast
  // window. Derived during render (the same "adjust state from props"
  // pattern used throughout this codebase's freshness code) so it applies
  // in the SAME render — no one-frame lag through an effect.
  if (ready !== prevReadyRef.current) {
    prevReadyRef.current = ready;
    if (!ready) windowStartRef.current = Date.now();
  }

  const refresh = useCallback(() => {
    windowStartRef.current = Date.now();
    void mutate();
  }, [mutate]);

  // Raw setInterval backstop (see module doc) — a completely separate timer
  // from SWR's self-rescheduling chain, so a stalled chain (whatever the
  // cause) doesn't leave the loop dead. Visibility-gated: skipped while
  // genuinely hidden; an occluded-but-visible tab is NOT hidden, so this
  // keeps ticking through exactly the throttling scenario the research
  // verified live.
  useEffect(() => {
    if (!key) return;
    const id = setInterval(() => {
      if (visibility && !visibility.isVisible()) return;
      const cadence = cadenceFor(ready, windowStartRef.current, Date.now());
      if (cadence === 0) return; // settled ready — backstop has nothing to do
      if (Date.now() - lastAttemptRef.current > cadence + BACKSTOP_SLACK_MS) {
        void mutate();
      }
    }, BACKSTOP_TICK_MS);
    return () => clearInterval(id);
  }, [key, mutate, visibility, ready]);

  // Resume-on-visible: an immediate revalidation the instant the tab/app
  // transitions to visible. Covers a genuinely hidden->visible transition —
  // a merely-occluded tab never fires this (visibility never changed there),
  // which is exactly what the backstop above is for.
  useEffect(() => {
    if (!key || !visibility) return;
    return visibility.subscribe(() => {
      if (visibility.isVisible()) void mutate();
    });
  }, [key, visibility, mutate]);

  // Last outcome that actually CLASSIFIED. The fetcher above is
  // rejection-safe by design — it resolves failures instead of throwing, so
  // the poll loop can never die on an unhandled rejection (see module doc).
  // The cost of that is invisible until you look at SWR's semantics: a
  // RESOLVED value is DATA, so a failed poll OVERWRITES the last good
  // response, where a thrown error would have sat alongside it in `error`
  // and left `data` intact. Net effect without this ref: one blip (a
  // transient 5xx, or the AbortController firing on a slow cold-start poll)
  // blanks `classified` for a single cadence tick, every consumer reading it
  // falls back to a loading state, and any UI mounted on top — the first-run
  // funnel most visibly — UNMOUNTS, taking the user's in-progress typing
  // with it, then remounts blank seconds later.
  //
  // So: keep the rejection-safety (it protects the loop) and add the
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
  // effect, not during render — `publishRuntimeHost` notifies subscribers synchronously. It
  // carries `statusScope`, so a body that lands after a workspace switch can never be read as
  // the new workspace's answer.
  // Defensive about the body exactly as `derivePhaseFromOutcome` is: an empty or malformed
  // 200 parses to `null` (fetchJsonWithStatus nulls out a JSON parse failure).
  const body = classified?.kind === "container" ? classified.body : undefined;
  const runtimeHost = body && typeof body === "object" ? body.runtime_host : undefined;
  useEffect(() => {
    if (statusScope && runtimeHost) publishRuntimeHost(statusScope, runtimeHost);
  }, [statusScope, runtimeHost]);
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
