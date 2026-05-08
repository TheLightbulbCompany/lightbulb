"use client";

import { useCallback, useEffect, useState } from "react";
import useSWR from "swr";
import { useAuth } from "@clerk/nextjs";
import { ApiError, BACKEND_URL } from "@/lib/api";

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

export type ProvisioningPhase = "loading" | "normal" | "blocked" | "team-pending";

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
  | { kind: "team-pending"; detail: string };

/**
 * Owns the chat-page centerpiece state machine:
 *
 *   /status load
 *        |
 *  +-----+------+
 *  |   200      | 202                    | 402
 *  v            v                         v
 *  normal     team-pending              blocked
 *
 * 200 covers both the "have a container" case and the "owner has no
 * container yet, gate is clear, backend kicked off provisioning"
 * case (status="provisioning"). 202 is returned only for org members
 * waiting on their admin-owned workspace. 404 is no longer a valid
 * response shape on this endpoint after backend Task 8.
 *
 * While blocked, polls fast (5s) for the first minute, then 30s.
 * `refresh()` resets to 5s and re-polls immediately.
 */
export function useProvisioningState(): ProvisioningStateResult {
  const { isSignedIn, getToken } = useAuth();
  // Two-phase blocked-state polling cadence: 5s for the first minute, then
  // 30s. `blockedFastPhase` is a *generation counter* — each new blocked
  // entry (or manual refresh) bumps it, the effect below tracks generations
  // and flips the cadence flag after 60s. Storing as a counter keeps the
  // effect derived purely from inputs (no setState-from-effect against the
  // same flag the effect reads) so `react-hooks/set-state-in-effect` is happy.
  const [blockedGen, setBlockedGen] = useState<number>(0);
  const [slowAfterGen, setSlowAfterGen] = useState<number>(-1);

  // We do a raw fetch here (rather than `useApi().get`) because the
  // /container/status endpoint encodes meaningful state in the HTTP
  // status code itself (200 / 202 / 402), and `useApi.get` collapses
  // 2xx responses into "just return body" — losing the 202 signal that
  // distinguishes "owner provisioning has been kicked off" (200 with
  // status="provisioning") from "org member, admin's workspace isn't
  // ready yet" (202 with detail="team workspace pending").
  const fetcher = useCallback(
    async (url: string): Promise<FetchResult> => {
      const token = await getToken();
      if (!token) throw new Error("No authentication token available");
      const response = await fetch(`${BACKEND_URL}${url}`, {
        method: "GET",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
      });
      const ct = response.headers.get("Content-Type") ?? "";
      let body: unknown = null;
      if (ct.includes("application/json")) {
        try {
          body = await response.json();
        } catch {
          // empty/malformed JSON — leave body as null
        }
      }

      if (response.status === 202) {
        // Backend Task 8: org members whose admin-owned workspace
        // hasn't been provisioned yet get 202 Accepted with a
        // {"detail": "..."} message instead of an immediate provision
        // attempt. The stepper renders a "waiting on your admin"
        // empty-state and keeps polling (5s) so we transition to
        // `normal` automatically once the admin's container appears.
        const detail =
          body && typeof body === "object"
            ? String((body as Record<string, unknown>).detail ?? "team workspace pending")
            : "team workspace pending";
        return { kind: "team-pending", detail };
      }

      if (response.ok) {
        return { kind: "container", data: body as ContainerInfo };
      }

      if (response.status === 402) {
        // FastAPI's HTTPException(detail=...) serializes to
        // {"detail": <whatever was passed>}. Backend passes
        // gate.to_payload() which is {"blocked": {...}}, so the wire
        // shape is {"detail": {"blocked": {...}}}. Read from the
        // detail envelope; fall back to a top-level "blocked" key
        // (used by some test fixtures and to be defensive about
        // transport quirks) so this hook stays robust either way.
        const envelope =
          (body && typeof body === "object"
            ? (body as Record<string, unknown>)
            : {}) || {};
        const detail = envelope.detail;
        const blocked =
          detail && typeof detail === "object" && "blocked" in (detail as object)
            ? (detail as { blocked: BlockedPayload }).blocked
            : (envelope.blocked as BlockedPayload | undefined);
        // Defensive: 402 without a recognizable blocked payload — throw
        // an ApiError so we don't silently render an empty blocked
        // screen.
        if (!blocked) throw new ApiError(response.status, body);
        return { kind: "blocked", data: blocked };
      }

      // Other non-2xx statuses propagate as ApiError so consumers see
      // a real error rather than a silent stuck UI.
      throw new ApiError(response.status, body);
    },
    [getToken],
  );

  const { data, mutate } = useSWR<FetchResult>(
    isSignedIn ? "/container/status" : null,
    fetcher,
    {
      revalidateOnFocus: false,
      // Function form lets SWR pass the latest data; while blocked,
      // fast-poll for the first minute (slowAfterGen < blockedGen means
      // the 60s timer hasn't fired yet for the current generation), then
      // drop to 30s. While `team-pending`, poll every 5s indefinitely so
      // the member's UI flips to `normal` shortly after their admin's
      // container appears. Avoids referencing `data` in this options
      // block (it isn't in scope yet).
      refreshInterval: (latest) => {
        if (latest?.kind === "blocked") {
          return slowAfterGen < blockedGen ? 5_000 : 30_000;
        }
        if (latest?.kind === "team-pending") return 5_000;
        return 0;
      },
    },
  );

  // The timer effect schedules a fresh 60s timeout each time the
  // *generation* changes while blocked. `blockedGen` is bumped:
  //   - on the rising edge into blocked (tracked by a setState-in-render
  //     pattern that triggers exactly one extra render at the transition;
  //     React handles this idiomatically — see "Storing information from
  //     previous renders" in the React docs)
  //   - on every `refresh()` call
  //
  // Codex P2 on PR #519: the previous `[isBlocked]`-only effect never
  // re-fired on subsequent refreshes, so a manual refresh after the
  // first minute could leave polling stuck at 5s indefinitely.
  const isBlocked = data?.kind === "blocked";
  const [prevIsBlocked, setPrevIsBlocked] = useState(false);
  if (isBlocked !== prevIsBlocked) {
    // setState-during-render is the React-recommended way to derive a
    // value from a prop/state change without an extra round-trip
    // through useEffect. It also keeps us clear of
    // `react-hooks/set-state-in-effect`. Only the "rising edge" bumps
    // the generation; the falling edge just resets the tracker.
    setPrevIsBlocked(isBlocked);
    if (isBlocked) setBlockedGen((g) => g + 1);
  }

  useEffect(() => {
    if (!isBlocked) return;
    const myGen = blockedGen;
    const t = setTimeout(() => setSlowAfterGen(myGen), 60_000);
    return () => clearTimeout(t);
  }, [isBlocked, blockedGen]);

  const refresh = useCallback(() => {
    // Manual refresh resets the fast-poll window: bump the generation so
    // (a) `slowAfterGen < blockedGen` again (back to 5s), and (b) the
    // timer effect re-fires for the new generation and schedules a fresh
    // 60s timeout.
    setBlockedGen((g) => g + 1);
    mutate();
  }, [mutate]);

  const refreshInterval = isBlocked ? (slowAfterGen < blockedGen ? 5_000 : 30_000) : 0;

  if (!data) {
    return { phase: "loading", container: null, blocked: null, refreshInterval: 0, refresh };
  }
  if (data.kind === "container") {
    return { phase: "normal", container: data.data, blocked: null, refreshInterval, refresh };
  }
  if (data.kind === "team-pending") {
    // Members poll fast (5s) so they see the workspace come online
    // shortly after their admin finishes provisioning. SWR's
    // refreshInterval is computed from the latest FetchResult above
    // (only `blocked` triggers the 5s/30s cadence), so we set the
    // returned `refreshInterval` here directly for any consumer that
    // wants to mirror the polling cadence in their own UI.
    return {
      phase: "team-pending",
      container: null,
      blocked: null,
      refreshInterval: 5_000,
      refresh,
    };
  }
  return { phase: "blocked", container: null, blocked: data.data, refreshInterval, refresh };
}
