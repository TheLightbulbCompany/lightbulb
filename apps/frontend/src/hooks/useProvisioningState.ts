"use client";

import { useCallback, useEffect, useState } from "react";
import useSWR from "swr";
import { useAuth } from "@clerk/nextjs";
import { ApiError, fetchJsonWithStatus } from "@/lib/api";
import { isPresent } from "@/lib/presence";
import { classifyContainerStatus } from "@isol8/shared";

/** Server-rendered blocked-state payload from /container/status (402). */
export interface BlockedPayload {
  code: string;
  title: string;
  message: string;
  action: {
    kind: "link";
    label: string;
    href: string;
    admin_only: boolean;
  };
  owner_role: "admin" | "member";
}

/**
 * Cold-start phase from `routers/container.py::_resolve_cold_start_phase`.
 * Declared here so `ContainerInfo.phase` is concretely typed (the index
 * signature `[key: string]: unknown` would otherwise widen `phase` to
 * `unknown` and break the cold-start phase-transition tracking in
 * ProvisioningStepper).
 */
export type ColdStartPhase = "provisioning" | "starting" | "ready" | "parked";

export interface ContainerInfo {
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
  /** Per-container onboarding fact: true once the first-run funnel has been
   * completed for this container (an org shares one, so it's org-wide). The
   * funnel gate reads this so a second org member never re-sees it. Optional
   * for backwards compat with old backends. */
  onboarded?: boolean;
  // Other /container/status response fields preserved for downstream use.
  [key: string]: unknown;
}

export type ProvisioningPhase =
  | "loading"
  | "normal"
  | "blocked"
  | "team-pending"
  | "needs-signup";

export interface ProvisioningStateOptions {
  /** Polling cadence to use when NOT in a pending state. Default 0 (no auto-poll).
   * The pending-state cadence (5s for the first minute, 30s after) always wins
   * over this when data.kind is blocked / team-pending / needs-signup. */
  refreshInterval?: number;
  /** When false, the hook does not fetch. Default true. */
  enabled?: boolean;
  /** Whether to revalidate on tab focus. Default false. */
  revalidateOnFocus?: boolean;
}

export interface ProvisioningStateResult {
  phase: ProvisioningPhase;
  container: ContainerInfo | null;
  blocked: BlockedPayload | null;
  refreshInterval: number;
  refresh: () => void;
  /** SWR error for non-2xx, non-handled statuses. */
  error: Error | undefined;
  /** SWR isLoading. */
  isLoading: boolean;
}

type FetchResult =
  | { kind: "container"; data: ContainerInfo }
  | { kind: "blocked"; data: BlockedPayload }
  | { kind: "team-pending"; detail: string }
  | { kind: "needs-signup" };

/**
 * Owns the chat-page centerpiece state machine:
 *
 *   /status load
 *        |
 *  +-----+------+--------------+--------------+
 *  |   200      | 202          | 402          | 404
 *  v            v              v              v
 *  normal     team-pending   blocked        needs-signup
 *
 * 200 covers both the "have a container" case and the "owner has no
 * container yet, gate is clear, backend kicked off provisioning"
 * case (status="provisioning"). 202 is returned only for org members
 * waiting on their admin-owned workspace. 404 is the explicit
 * "owner has never started signup" signal — the backend returns it
 * for admins whose provision gate would fire `signup_required`, so
 * the chat surface can render the ProviderPicker (the only checkout
 * entry point) without trapping the user in a Subscribe button that
 * loops back here. Inactive-row admins (canceled, unpaid, paused)
 * still get 402 with a /settings/billing action.
 *
 * While in either `blocked` OR `team-pending`, polls fast (5s) for the
 * first minute, then 30s. `refresh()` resets to 5s and re-polls
 * immediately. Without the slow phase, a member whose admin never
 * provisions would hammer /container/status indefinitely (720 req/hour).
 * `needs-signup` polls on the same cadence as `blocked` / `team-pending`
 * so a user returning from Stripe Checkout before the
 * customer.subscription.created webhook lands doesn't get stuck on a
 * stale picker. The gate briefly returns 404 in that window
 * (status=None on the billing row), and only revalidation will pick
 * up the eventual transition to `normal`.
 */
export function useProvisioningState(
  options: ProvisioningStateOptions = {},
): ProvisioningStateResult {
  const { refreshInterval: consumerInterval = 0, enabled = true, revalidateOnFocus = false } = options;
  const { isSignedIn, getToken } = useAuth();
  // Two-phase pending-state polling cadence (covers both `blocked` and
  // `team-pending`): 5s for the first minute, then 30s. `pendingGen` is a
  // *generation counter* — each new pending entry (or manual refresh)
  // bumps it; the effect below tracks generations and flips the cadence
  // flag after 60s. Storing as a counter keeps the effect derived purely
  // from inputs (no setState-from-effect against the same flag the effect
  // reads) so `react-hooks/set-state-in-effect` is happy.
  const [pendingGen, setPendingGen] = useState<number>(0);
  const [slowAfterGen, setSlowAfterGen] = useState<number>(-1);

  // Uses fetchJsonWithStatus (not `useApi().get`) because the
  // /container/status endpoint encodes meaningful state in the HTTP
  // status code itself (200 / 202 / 402), and `useApi.get` collapses
  // 2xx responses into "just return body" — losing the 202 signal that
  // distinguishes "owner provisioning has been kicked off" (200 with
  // status="provisioning") from "org member, admin's workspace isn't
  // ready yet" (202 with detail="team workspace pending").
  const fetcher = useCallback(
    async (url: string): Promise<FetchResult> => {
      try {
        // Wake marker, computed at FETCH time (not baked into the SWR key —
        // key stability keeps dedupe/mutate semantics intact). wake=1 only
        // while a human is plausibly present (visible + input < 120s);
        // wake=0 otherwise, which tells the backend to report a parked
        // trial container instead of starting it. Query param by design —
        // a custom header would fail CORS preflight against an older
        // backend during deploy skew.
        const sep = url.includes("?") ? "&" : "?";
        const wakeUrl = `${url}${sep}wake=${isPresent() ? "1" : "0"}`;
        const { status, body } = await fetchJsonWithStatus(wakeUrl, getToken);
        // Status-code + payload-shape parse lives in @isol8/shared (mobile
        // onboarding-parity Slice 1 — shared with mobile's containerStatus.ts,
        // which used to be a hand-ported, drifting copy of this logic). This
        // hook keeps everything else: the SWR binding, the fast/slow poll
        // cadence, and the policies below for each classified kind.
        const classified = classifyContainerStatus({ status, body });
        if (classified.kind === "team-pending") {
          // Backend Task 8: org members whose admin-owned workspace
          // hasn't been provisioned yet get 202 Accepted with a
          // {"detail": "..."} message instead of an immediate provision
          // attempt. The stepper renders a "waiting on your admin"
          // empty-state and keeps polling so we transition to `normal`
          // automatically once the admin's container appears.
          return { kind: "team-pending", detail: classified.detail ?? "team workspace pending" };
        }
        if (classified.kind === "container") {
          // 2xx default: container payload, passed through raw by the
          // classifier. Cast from `body` (not `classified.body`) — same
          // `unknown`-sourced cast this hook always used; ContainerInfo and
          // the shared ContainerStatusBody are deliberately different
          // (stricter) shapes so this cast stays exactly as permissive as
          // it was before the rewire.
          return { kind: "container", data: body as ContainerInfo };
        }
        // This endpoint only ever answers with 200 or 202 without throwing
        // (fetchJsonWithStatus throws on any other status), so `classified`
        // can't reach here in production — but the type can't prove that.
        // Treat it exactly like any other unrecognized response below.
        throw new Error(`Unexpected /container/status classification: ${classified.kind}`);
      } catch (err) {
        if (!(err instanceof ApiError)) throw err;
        // 402 / 404 / anything else: same status-code table as the 2xx
        // path above, fed from the caught ApiError's status + body.
        const classified = classifyContainerStatus({ status: err.status, body: err.body });
        if (classified.kind === "needs-signup") {
          // Backend signal for "owner has never started signup": gate
          // would fire `signup_required` for an admin, but the
          // /container/status endpoint converts that to 404 so the
          // chat surface renders the ProviderPicker (which calls
          // POST /billing/trial-checkout — the only way to create the
          // first billing row) instead of trapping the admin in a
          // Subscribe-loop UI. Inactive-row admins still get 402.
          return { kind: "needs-signup" };
        }
        if (classified.kind === "blocked") {
          return { kind: "blocked", data: classified.blocked as BlockedPayload };
        }
        // classified.kind === "unrecognized" — either a non-402/404 status,
        // or a 402 without a recognizable blocked payload (FastAPI's
        // HTTPException(detail=gate.to_payload()) always sends one in
        // practice). Let the ORIGINAL ApiError propagate (not a
        // reconstructed one — preserves its message/requestId) so
        // consumers see a real error rather than a silently stuck UI.
        throw err;
      }
    },
    [getToken],
  );

  const { data, mutate, error, isLoading } = useSWR<FetchResult>(
    isSignedIn && enabled ? "/container/status" : null,
    fetcher,
    {
      revalidateOnFocus,
      // Function form lets SWR pass the latest data. While in any
      // pending state (blocked / team-pending / needs-signup),
      // fast-poll for the first minute (`slowAfterGen < pendingGen`
      // means the 60s timer hasn't fired yet for the current
      // generation), then drop to 30s. needs-signup polls so a user
      // returning from Stripe before the subscription webhook lands
      // doesn't get stuck on a stale picker. Avoids referencing `data`
      // in this options block (it isn't in scope yet).
      refreshInterval: (latest) => {
        if (
          latest?.kind === "blocked" ||
          latest?.kind === "team-pending" ||
          latest?.kind === "needs-signup"
        ) {
          return slowAfterGen < pendingGen ? 5_000 : 30_000;
        }
        return consumerInterval;
      },
    },
  );

  // The timer effect schedules a fresh 60s timeout each time the
  // *generation* changes while pending. `pendingGen` is bumped:
  //   - on the rising edge into pending (blocked OR team-pending),
  //     tracked by a setState-in-render pattern that triggers exactly
  //     one extra render at the transition; React handles this
  //     idiomatically — see "Storing information from previous renders"
  //     in the React docs
  //   - on every `refresh()` call
  //
  // An `[isBlocked]`-only effect would not re-fire on subsequent refreshes,
  // leaving polling stuck at 5s indefinitely after a manual refresh past
  // the first minute.
  const isPending =
    data?.kind === "blocked" ||
    data?.kind === "team-pending" ||
    data?.kind === "needs-signup";
  const [prevIsPending, setPrevIsPending] = useState(false);
  if (isPending !== prevIsPending) {
    // setState-during-render is the React-recommended way to derive a
    // value from a prop/state change without an extra round-trip
    // through useEffect. It also keeps us clear of
    // `react-hooks/set-state-in-effect`. Only the "rising edge" bumps
    // the generation; the falling edge just resets the tracker.
    setPrevIsPending(isPending);
    if (isPending) setPendingGen((g) => g + 1);
  }

  useEffect(() => {
    if (!isPending) return;
    const myGen = pendingGen;
    const t = setTimeout(() => setSlowAfterGen(myGen), 60_000);
    return () => clearTimeout(t);
  }, [isPending, pendingGen]);

  const refresh = useCallback(() => {
    // Manual refresh resets the fast-poll window: bump the generation so
    // (a) `slowAfterGen < pendingGen` again (back to 5s), and (b) the
    // timer effect re-fires for the new generation and schedules a fresh
    // 60s timeout.
    setPendingGen((g) => g + 1);
    mutate();
  }, [mutate]);

  const refreshInterval = isPending
    ? slowAfterGen < pendingGen
      ? 5_000
      : 30_000
    : consumerInterval;

  if (!data) {
    return { phase: "loading", container: null, blocked: null, refreshInterval: consumerInterval, refresh, error, isLoading };
  }
  if (data.kind === "container") {
    return { phase: "normal", container: data.data, blocked: null, refreshInterval, refresh, error, isLoading };
  }
  if (data.kind === "team-pending") {
    // Members poll fast (5s) at first so they see the workspace come
    // online shortly after their admin finishes provisioning, then drop
    // to 30s after a minute so a member whose admin never provisions
    // doesn't hammer the endpoint indefinitely.
    return {
      phase: "team-pending",
      container: null,
      blocked: null,
      refreshInterval,
      refresh,
      error,
      isLoading,
    };
  }
  if (data.kind === "needs-signup") {
    // Polls on the same cadence as blocked/team-pending so a user
    // returning from Stripe Checkout before customer.subscription.created
    // fires (which flips status from None to "trialing"/"active") gets
    // picked up automatically rather than stuck on a stale picker.
    return { phase: "needs-signup", container: null, blocked: null, refreshInterval, refresh, error, isLoading };
  }
  return { phase: "blocked", container: null, blocked: data.data, refreshInterval, refresh, error, isLoading };
}
