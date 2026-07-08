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

export type ContainerStatus =
  | { kind: "loading" }
  | { kind: "ready" }
  | { kind: "starting"; phase?: string; substatus?: string }
  | { kind: "teamPending" }
  | {
      kind: "blocked";
      code: string;
      title: string;
      message: string | null;
      href: string | null;
      adminOnly: boolean;
    }
  | { kind: "needsSignup" }
  | { kind: "unreachable"; message: string };

interface BlockedPayload {
  code?: string;
  title?: string;
  message?: string;
  action?: { href?: string; admin_only?: boolean };
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
      const { status: code, body } = await fetchJsonWithStatus(
        config,
        "/container/status",
      );
      if (code === 202) {
        next = { kind: "teamPending" };
      } else {
        const info = (body ?? {}) as StatusBody;
        // phase absent (older backend / plain running box) → treat as ready.
        if (info.phase == null || info.phase === "ready") {
          next = { kind: "ready" };
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

  // Poll only while transient (starting / team-pending). The dep is
  // `status.kind`, so the interval is torn down + rebuilt only on a state
  // TRANSITION — never once per tick — and stops entirely once ready/blocked.
  useEffect(() => {
    if (status.kind !== "starting" && status.kind !== "teamPending") return;
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
