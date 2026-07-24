"use client";

import { useAuth } from "@clerk/nextjs";
import { useOnboardingStatus, type StatusContract } from "@isol8/shared";

import { recordRequestId } from "@/lib/requestIdLog";
import { isPresent } from "@/lib/presence";
import { webVisibilityAdapter } from "@/lib/visibility";

// Deliberately NOT `import { BACKEND_URL } from "@/lib/api"`: this hook is
// mounted (transitively, via ChatLayout/OverviewPanel/useSystemHealth) by
// ~20 existing test suites that `vi.mock("@/lib/api", () => ({ useApi: ...
// }))` with a minimal factory carrying no `BACKEND_URL` export — Vitest's
// mock-export guard throws on ANY read of a name the factory didn't return,
// even a plain constant. Recomputing the identical fallback expression
// locally (verbatim from `@/lib/api`'s own `BACKEND_URL`) avoids widening
// that mock surface for every one of those unrelated suites.
const BACKEND_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api/v1";

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
 * The composed `StatusContract` — the ONE wire shape `/container/status`
 * returns (spec PR-9, CLIENT-HOOK). No legacy flat duplicates exist on the
 * wire anymore (`phase`/`status`/`substatus`/`teams_status` were deleted
 * from `routers/container.py::_status_response` in this PR) — every reader
 * downstream of this hook reads the contract fields (`runtime_phase`,
 * `lifecycle`, `container_status`, `container_substatus`, `teams_state`,
 * `chat_available`, `onboarded`, `funnel`, `bootstrap`, `generations`)
 * directly, or via `deriveUIPhase`.
 */
export type ContainerInfo = StatusContract;

export type ProvisioningPhase =
  | "loading"
  | "normal"
  | "blocked"
  | "team-pending"
  | "needs-signup";

export interface ProvisioningStateOptions {
  /** When false, the hook does not fetch. Default true. */
  enabled?: boolean;
}

export interface ProvisioningStateResult {
  phase: ProvisioningPhase;
  container: ContainerInfo | null;
  blocked: BlockedPayload | null;
  refresh: () => void;
  /** A genuine transport failure, OR a non-2xx/402/404/202 status this
   * classifier doesn't have a phase for (mirrors the pre-CLIENT-HOOK
   * behavior of throwing on `unrecognized` so a real error surfaces
   * instead of a silently stuck UI). */
  error: Error | undefined;
  isLoading: boolean;
}

/**
 * Owns the chat-page centerpiece state machine — now a PURE READER over the
 * ONE shared `useOnboardingStatus` hook (spec PR-9, CLIENT-HOOK; INV-1: the
 * hook never re-derives readiness, it only classifies the composed
 * `StatusContract` response). This collapses what used to be this hook's OWN
 * SWR instance — with its own two-phase (5s/30s) pending-cadence state
 * machine, its own `pendingGen`/`slowAfterGen` timers, and a per-consumer
 * `refreshInterval`/`revalidateOnFocus` override — into a single shared
 * fetch + a single shared bounded backstop loop that every consumer (this
 * hook, `useSystemHealth`, `OverviewPanel`) now rides for free. Consumer-
 * supplied polling cadence is gone: there is exactly one cadence for the
 * whole app (5s while unsettled and within the first 60s, 15s after,
 * 0/off once ready — see `useOnboardingStatus`'s module doc).
 *
 *   /status load
 *        |
 *  +-----+------+--------------+--------------+
 *  |   200      | 202          | 402          | 404
 *  v            v              v              v
 *  normal     team-pending   blocked        needs-signup
 *
 * 200 covers both the "have a container" case and the "owner has no
 * container yet, gate is clear, backend kicked off provisioning" case
 * (`runtime_phase: "provisioning"`). 202 is returned only for org members
 * waiting on their admin-owned workspace. 404 is the explicit "owner has
 * never started signup" signal — the backend returns it for admins whose
 * provision gate would fire `signup_required`, so the chat surface can
 * render the ProviderPicker (the only checkout entry point) without
 * trapping the user in a Subscribe button that loops back here.
 * Inactive-row admins (canceled, unpaid, paused) still get 402 with a
 * /settings/billing action.
 */
export function useProvisioningState(
  options: ProvisioningStateOptions = {},
): ProvisioningStateResult {
  const { enabled = true } = options;
  const { isSignedIn, getToken } = useAuth();

  const { classified, networkError, isLoading, refresh } = useOnboardingStatus({
    api: { apiUrl: BACKEND_URL, getToken, onRequestId: recordRequestId },
    enabled: !!isSignedIn && enabled,
    // Wake marker, computed at FETCH time (not baked into the SWR key —
    // key stability keeps dedupe/mutate semantics intact). wake=1 only
    // while a human is plausibly present (visible + input < 120s); wake=0
    // otherwise, which tells the backend to report a parked trial
    // container instead of starting it.
    wake: () => isPresent(),
    visibility: webVisibilityAdapter,
  });

  if (!classified) {
    return { phase: "loading", container: null, blocked: null, refresh, error: networkError, isLoading };
  }

  switch (classified.kind) {
    case "container":
      return {
        phase: "normal",
        container: classified.body as unknown as ContainerInfo,
        blocked: null,
        refresh,
        error: networkError,
        isLoading,
      };
    case "team-pending":
      // Backend Task 8: org members whose admin-owned workspace hasn't
      // been provisioned yet get 202 Accepted. The stepper renders a
      // "waiting on your admin" empty-state; the shared bounded loop
      // keeps polling so we transition to `normal` automatically once the
      // admin's container appears.
      return { phase: "team-pending", container: null, blocked: null, refresh, error: networkError, isLoading };
    case "needs-signup":
      // "Owner has never started signup": gate would fire signup_required
      // for an admin, but /container/status converts that to 404 so the
      // chat surface renders the ProviderPicker (which calls
      // POST /billing/trial-checkout — the only way to create the first
      // billing row) instead of trapping the admin in a Subscribe-loop UI.
      return { phase: "needs-signup", container: null, blocked: null, refresh, error: networkError, isLoading };
    case "blocked":
      return {
        phase: "blocked",
        container: null,
        blocked: classified.blocked as BlockedPayload,
        refresh,
        error: networkError,
        isLoading,
      };
    case "unrecognized":
    default:
      // Either a non-402/404 status, or a 402 without a recognizable
      // blocked payload (FastAPI's HTTPException(detail=gate.to_payload())
      // always sends one in practice). Surface a real error rather than a
      // silently stuck UI — mirrors the pre-CLIENT-HOOK behavior of
      // letting the ApiError propagate.
      return {
        phase: "loading",
        container: null,
        blocked: null,
        refresh,
        error: networkError ?? new Error(`Unexpected /container/status status: ${classified.status}`),
        isLoading,
      };
  }
}
