/**
 * Provision-gate / onboarding readiness probe (W5 contract §B).
 *
 * `GET /container/status` encodes state in the HTTP STATUS CODE, not just the
 * body — so we read it via `fetchJsonWithStatus` (200 vs 202) and catch
 * `ApiError` for 402 (blocked gate) and 404 (needs-signup). The status-code +
 * payload-shape parse itself lives in `@isol8/shared`'s `classifyContainerStatus`
 * (mobile onboarding-parity Slice 1) — this file's job is ONLY the React
 * binding (`useState` + `AppState`, 5s poll) and turning a `ClassifiedStatus`
 * into this file's richer `ContainerStatus` (which additionally distinguishes
 * ready / starting / parked / error from the 200 body's `status`/`phase`
 * fields — a rendering decision the shared classifier deliberately leaves to
 * each platform). Classification (contract §B1/§B3):
 *
 *   ready       — 200 with phase === "ready" (openclaw responsive)
 *   starting    — 200 with phase !== "ready" (cold start / provisioning)
 *   parked      — 200 with phase === "parked" (deliberately-asleep trial box)
 *   error       — 200 with raw status === "error" (mobile onboarding-parity
 *                 Slice 1 — the backend reports phase="provisioning" for a
 *                 failed container too, so this check runs BEFORE the phase
 *                 branches; matches web's ProvisioningStepper `container.status
 *                 === "error"` check)
 *   teamPending — 202 (org member waiting on admin's workspace)
 *   blocked     — 402 (provision gate fired; carries code + action.href + admin_only)
 *   needsSignup — 404 (admin never started signup)
 *   unreachable — network / auth / unexpected error, OR an unrecognized
 *                 status code the shared classifier couldn't place
 *
 * getTokenRef REST pattern (control/overview.tsx lesson): pin Clerk's churning
 * `getToken` behind a ref so the adapter config is built exactly once. The
 * poll interval reads the latest status via `status.kind` in its effect dep —
 * NOT by recreating the api client each tick.
 *
 * `ready` (and `starting`, when the body carries one) also surfaces the
 * per-CONTAINER `onboarded` fact straight from the body (mobile
 * onboarding-parity Slice 2) — see `containerStatusContext.ts` for how
 * FirstRunGate reads this SAME poll instead of running a second one.
 * `undefined` means "not known" (old backend / not-yet-ready) and callers
 * must treat that as onboarded-enough to skip the funnel, never as false.
 */
import { useAuth } from "@clerk/clerk-expo";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AppState } from "react-native";
import {
  ApiError,
  classifyContainerStatus,
  fetchJsonWithStatus,
  type ClassifiedStatus,
  type ContainerStatusBody,
} from "@isol8/shared";

import { makeAdapter } from "./adapter";
import { isPresent } from "./presence";

export type ContainerStatus =
  | { kind: "loading" }
  | { kind: "ready"; onboarded?: boolean }
  | { kind: "starting"; phase?: string; substatus?: string; onboarded?: boolean }
  | { kind: "parked" }
  /** Backend-reported container failure (mobile onboarding-parity Slice 1 —
   * mobile previously had no surface for this and showed `starting` forever;
   * unifies with web's "Setup failed" card, which keys on the same raw
   * `status === "error"`). */
  | { kind: "error" }
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
      /**
       * The polling user's role on this owner's workspace ("admin" | "member"),
       * from the gate payload's `owner_role` (mobile onboarding-parity Slice 4
       * fix) — same optional-surfacing pattern this file already uses for
       * `onboarded`. `adminOnly` alone can't tell an org admin apart from a
       * member; screens that gate on admin-only actions need both. Undefined
       * on an old backend / unexpected value — callers must treat that as
       * "unknown", never coerce it to a specific role.
       */
      ownerRole?: "admin" | "member";
    }
  | { kind: "needsSignup" }
  | { kind: "unreachable"; message: string };

const POLL_INTERVAL_MS = 5_000;

/**
 * Turns the shared classifier's status-code-level result into this file's
 * richer `ContainerStatus`. `unreachableMessage` is only used for the
 * `unrecognized` branch — the caller supplies `err.message` when `classified`
 * came from a caught `ApiError`, or a generic fallback for the (practically
 * unreachable — this endpoint only ever answers 200/202 on success)
 * unrecognized-2xx case.
 */
function fromClassified(
  classified: ClassifiedStatus,
  unreachableMessage: string,
): ContainerStatus {
  switch (classified.kind) {
    case "team-pending":
      return { kind: "teamPending" };
    case "needs-signup":
      return { kind: "needsSignup" };
    case "blocked": {
      const blocked = classified.blocked;
      return {
        kind: "blocked",
        code: blocked.code ?? "blocked",
        title: blocked.title ?? "Action needed",
        message: blocked.message ?? null,
        href: blocked.action?.href ?? null,
        actionLabel: blocked.action?.label ?? null,
        adminOnly: Boolean(blocked.action?.admin_only),
        ownerRole:
          blocked.owner_role === "admin" || blocked.owner_role === "member"
            ? blocked.owner_role
            : undefined,
      };
    }
    case "unrecognized":
      return { kind: "unreachable", message: unreachableMessage };
    case "container": {
      // Defensive against an empty/malformed 200 body — mirrors this file's
      // pre-Slice-1 behavior. The classifier's `ContainerStatusBody` type
      // isn't nullable, but that's a type-level claim, not a runtime one.
      const info = classified.body ?? ({} as ContainerStatusBody);
      // Checked BEFORE the phase branches below: the backend reports
      // phase="provisioning" for a failed container too, so an error read
      // AFTER the phase checks would misrender as a perpetual "starting"
      // spinner (the exact gap this slice closes — see module docstring).
      if (info.status === "error") {
        return { kind: "error" };
      }
      // phase absent (older backend / plain running box) → treat as ready.
      if (info.phase == null || info.phase === "ready") {
        return { kind: "ready", onboarded: info.onboarded };
      }
      if (info.phase === "parked") {
        // Deliberately-asleep trial box (wake-gated). Distinct from a cold
        // start: it's healthy, one intent away from running, so it must NOT
        // render as a perpetual "starting…" spinner. Foregrounding already
        // fired a wake in parallel (gateway provider), so this is transient.
        return { kind: "parked" };
      }
      return {
        kind: "starting",
        phase: info.phase,
        substatus: info.substatus ?? undefined,
        onboarded: info.onboarded,
      };
    }
  }
}

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
      next = fromClassified(classifyContainerStatus({ status: code, body }), "Request failed");
    } catch (err) {
      if (err instanceof ApiError) {
        next = fromClassified(
          classifyContainerStatus({ status: err.status, body: err.body }),
          err.message,
        );
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
