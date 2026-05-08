"use client";

import { useCallback, useEffect, useState } from "react";
import useSWR from "swr";
import { useAuth } from "@clerk/nextjs";
import { ApiError, fetchJsonWithStatus } from "@/lib/api";

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

export interface ContainerInfo {
  service_name: string;
  status: string;
  substatus: string | null;
  // Other /container/status response fields preserved for downstream use.
  [key: string]: unknown;
}

export type ProvisioningPhase =
  | "loading"
  | "normal"
  | "blocked"
  | "team-pending"
  | "needs-signup";

export interface ProvisioningStateResult {
  phase: ProvisioningPhase;
  container: ContainerInfo | null;
  blocked: BlockedPayload | null;
  refreshInterval: number;
  refresh: () => void;
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
 * `needs-signup` does not auto-poll — the next state change comes from
 * the user clicking a picker card and being redirected to Stripe.
 */
export function useProvisioningState(): ProvisioningStateResult {
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
        const { status, body } = await fetchJsonWithStatus(url, getToken);
        if (status === 202) {
          // Backend Task 8: org members whose admin-owned workspace
          // hasn't been provisioned yet get 202 Accepted with a
          // {"detail": "..."} message instead of an immediate provision
          // attempt. The stepper renders a "waiting on your admin"
          // empty-state and keeps polling so we transition to `normal`
          // automatically once the admin's container appears.
          const detail =
            body && typeof body === "object"
              ? String((body as Record<string, unknown>).detail ?? "team workspace pending")
              : "team workspace pending";
          return { kind: "team-pending", detail };
        }
        // 2xx default: container payload.
        return { kind: "container", data: body as ContainerInfo };
      } catch (err) {
        if (!(err instanceof ApiError)) throw err;
        if (err.status === 404) {
          // Backend signal for "owner has never started signup": gate
          // would fire `signup_required` for an admin, but the
          // /container/status endpoint converts that to 404 so the
          // chat surface renders the ProviderPicker (which calls
          // POST /billing/trial-checkout — the only way to create the
          // first billing row) instead of trapping the admin in a
          // Subscribe-loop UI. Inactive-row admins still get 402.
          return { kind: "needs-signup" };
        }
        if (err.status === 402) {
          // FastAPI's HTTPException(detail=...) serializes to
          // {"detail": <whatever was passed>}. Backend passes
          // gate.to_payload() which is {"blocked": {...}}, so the wire
          // shape is {"detail": {"blocked": {...}}}. Read from the
          // detail envelope; fall back to a top-level "blocked" key
          // (used by some test fixtures and to be defensive about
          // transport quirks) so this hook stays robust either way.
          const body = err.body;
          const envelope =
            (body && typeof body === "object"
              ? (body as Record<string, unknown>)
              : {}) || {};
          const detail = envelope.detail;
          const blocked =
            detail && typeof detail === "object" && "blocked" in (detail as object)
              ? (detail as { blocked: BlockedPayload }).blocked
              : (envelope.blocked as BlockedPayload | undefined);
          // Defensive: 402 without a recognizable blocked payload — let
          // the ApiError propagate so we don't silently render an empty
          // blocked screen.
          if (!blocked) throw err;
          return { kind: "blocked", data: blocked };
        }
        // Other non-2xx statuses propagate as ApiError so consumers see
        // a real error rather than a silent stuck UI.
        throw err;
      }
    },
    [getToken],
  );

  const { data, mutate } = useSWR<FetchResult>(
    isSignedIn ? "/container/status" : null,
    fetcher,
    {
      revalidateOnFocus: false,
      // Function form lets SWR pass the latest data. While in either
      // `blocked` or `team-pending`, fast-poll for the first minute
      // (`slowAfterGen < pendingGen` means the 60s timer hasn't fired
      // yet for the current generation), then drop to 30s. Avoids
      // referencing `data` in this options block (it isn't in scope yet).
      refreshInterval: (latest) => {
        if (latest?.kind === "blocked" || latest?.kind === "team-pending") {
          return slowAfterGen < pendingGen ? 5_000 : 30_000;
        }
        return 0;
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
  // Codex P2 on PR #519: the previous `[isBlocked]`-only effect never
  // re-fired on subsequent refreshes, so a manual refresh after the
  // first minute could leave polling stuck at 5s indefinitely.
  const isPending = data?.kind === "blocked" || data?.kind === "team-pending";
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

  const refreshInterval = isPending ? (slowAfterGen < pendingGen ? 5_000 : 30_000) : 0;

  if (!data) {
    return { phase: "loading", container: null, blocked: null, refreshInterval: 0, refresh };
  }
  if (data.kind === "container") {
    return { phase: "normal", container: data.data, blocked: null, refreshInterval, refresh };
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
    };
  }
  if (data.kind === "needs-signup") {
    // No auto-poll. The user must click a picker card to advance —
    // that fires POST /billing/trial-checkout and redirects to Stripe;
    // SWR re-fetches when they return.
    return { phase: "needs-signup", container: null, blocked: null, refreshInterval: 0, refresh };
  }
  return { phase: "blocked", container: null, blocked: data.data, refreshInterval, refresh };
}
