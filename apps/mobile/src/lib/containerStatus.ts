/**
 * Provision-gate / onboarding readiness probe (W5 contract §B).
 *
 * `GET /container/status` encodes state in the HTTP STATUS CODE, not just the
 * body — so we read it via `fetchJsonWithStatus` (200 vs 202) and catch
 * `ApiError` for 402 (blocked gate) and 404 (needs-signup). Classification
 * (contract §B1/§B3):
 *
 *   ready       — 200 with phase === "ready" (openclaw responsive)
 *   starting    — 200 with phase !== "ready" (cold start / provisioning)
 *   teamPending — 202 (org member waiting on admin's workspace)
 *   blocked     — 402 (provision gate fired; carries code + action.href + admin_only)
 *   needsSignup — 404 (admin never started signup)
 *   unreachable — network / auth / unexpected error
 *
 * getTokenRef REST pattern (control/overview.tsx lesson): pin Clerk's churning
 * `getToken` behind a ref so the adapter config is built exactly once. The
 * poll interval reads the latest status via `status.kind` in its effect dep —
 * NOT by recreating the api client each tick.
 */
import { useAuth } from "@clerk/clerk-expo";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AppState } from "react-native";
import { ApiError, fetchJsonWithStatus } from "@isol8/shared";

import { makeAdapter } from "./adapter";
import { isPresent } from "./presence";

export type ContainerStatus =
  | { kind: "loading" }
  | { kind: "ready" }
  | { kind: "starting"; phase?: string; substatus?: string }
  | { kind: "parked" }
  | { kind: "teamPending" }
  | {
      kind: "blocked";
      code: string;
      title: string;
      message: string | null;
      href: string | null;
      /** Gate action label (e.g. "Upgrade", "Set up billing") — null when absent. */
      actionLabel: string | null;
      adminOnly: boolean;
    }
  | { kind: "needsSignup" }
  | { kind: "unreachable"; message: string };

interface BlockedPayload {
  code?: string;
  title?: string;
  message?: string;
  action?: { label?: string; href?: string; admin_only?: boolean };
}

interface StatusBody {
  status?: string;
  substatus?: string;
  phase?: string;
}

const POLL_INTERVAL_MS = 5_000;

export function useContainerStatus() {
  const { getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const config = useMemo(() => makeAdapter(() => getTokenRef.current()), []);

  const [status, setStatus] = useState<ContainerStatus>({ kind: "loading" });
  const seqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = ++seqRef.current;
    let next: ContainerStatus;
    try {
      // Wake marker: `wake=1` while the app is foregrounded (present → a poll
      // may wake a parked trial container), `wake=0` when backgrounded
      // (read-only, never start a parked box). Query param, not a header —
      // CORS-safe against a backend that predates the param, which just
      // ignores it. On an old backend `phase: "parked"` never comes back, so
      // the new branch below is inert there.
      const { status: code, body } = await fetchJsonWithStatus(
        config,
        `/container/status?wake=${isPresent() ? "1" : "0"}`,
      );
      if (code === 202) {
        next = { kind: "teamPending" };
      } else {
        const info = (body ?? {}) as StatusBody;
        // phase absent (older backend / plain running box) → treat as ready.
        if (info.phase == null || info.phase === "ready") {
          next = { kind: "ready" };
        } else if (info.phase === "parked") {
          // Deliberately-asleep trial box (wake-gated). Distinct from a cold
          // start: it's healthy, one intent away from running, so it must NOT
          // render as a perpetual "starting…" spinner. Foregrounding already
          // fired a wake in parallel (gateway provider), so this is transient.
          next = { kind: "parked" };
        } else {
          next = {
            kind: "starting",
            phase: info.phase,
            substatus: info.substatus,
          };
        }
      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 402) {
        const blocked =
          ((err.body as { detail?: { blocked?: BlockedPayload } } | null)
            ?.detail?.blocked ?? {}) as BlockedPayload;
        next = {
          kind: "blocked",
          code: blocked.code ?? "blocked",
          title: blocked.title ?? "Action needed",
          message: blocked.message ?? null,
          href: blocked.action?.href ?? null,
          actionLabel: blocked.action?.label ?? null,
          adminOnly: Boolean(blocked.action?.admin_only),
        };
      } else if (err instanceof ApiError && err.status === 404) {
        next = { kind: "needsSignup" };
      } else {
        next = {
          kind: "unreachable",
          message: err instanceof Error ? err.message : "Request failed",
        };
      }
    }
    if (seqRef.current !== seq) return;
    setStatus(next);
  }, [config]);

  // Initial probe on mount.
  useEffect(() => {
    void load();
  }, [load]);

  // Poll only while transient (starting / team-pending / parked). The dep is
  // `status.kind`, so the interval is torn down + rebuilt only on a state
  // TRANSITION — never once per tick — and stops entirely once ready/blocked.
  // `parked` polls too: a foreground poll carries wake=1, so it drives the box
  // from asleep → starting → ready without any user action.
  useEffect(() => {
    if (
      status.kind !== "starting" &&
      status.kind !== "teamPending" &&
      status.kind !== "parked"
    ) {
      return;
    }
    const id = setInterval(() => void load(), POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [status.kind, load]);

  // Re-check when the app returns to the foreground.
  useEffect(() => {
    const sub = AppState.addEventListener("change", (s) => {
      if (s === "active") void load();
    });
    return () => sub.remove();
  }, [load]);

  return { status, refetch: load };
}
