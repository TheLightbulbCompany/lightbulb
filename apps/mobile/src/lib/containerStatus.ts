/**
 * Provision-gate / onboarding readiness probe (W5 contract §B).
 *
 * `GET /container/status` encodes state in the HTTP STATUS CODE, not just the
 * body — so we read it via `fetchJsonWithStatus` (200 vs 202) and catch
 * `ApiError` for 402 (blocked gate) and 404 (needs-signup). The status-code +
 * payload-shape parse itself lives in `@isol8/shared`'s `classifyContainerStatus`
 * (mobile onboarding-parity Slice 1) — this file's job is ONLY the React
 * binding (`useState` + `AppState` + the poll loop) and turning a `ClassifiedStatus`
 * into this file's richer `ContainerStatus` (which additionally distinguishes
 * ready / starting / parked / error from the 200 body's `status`/`phase`
 * fields — a rendering decision the shared classifier deliberately leaves to
 * each platform) plus the poll loop's per-kind cadence (see
 * `pollIntervalFor`). Classification (contract §B1/§B3):
 *
 *   ready       — 200 with phase === "ready" (openclaw responsive)
 *   starting    — 200 with phase !== "ready" (cold start / provisioning), OR
 *                 the ONE non-terminal error shape: status === "error" with
 *                 substatus === "error_retrying" (backend is auto-retrying)
 *   parked      — 200 with phase === "parked" (deliberately-asleep trial box)
 *   error       — 200 with raw status === "error" and ANY substatus other than
 *                 "error_retrying" — provision_failed, deployment_failed (the
 *                 ECS rollout circuit breaker), a bare error with no substatus
 *                 (auto-retry gated / standing down), or any unknown value —
 *                 i.e. a TERMINAL failure (PR3 honest-contract consumption).
 *                 CRITICAL: `phase` still resolves to "provisioning" for a dead
 *                 container too, so terminal-ness is keyed off `substatus`,
 *                 NEVER `phase` — keying off phase would render a dead
 *                 container as a perpetual stepper. Only the explicit
 *                 "error_retrying" signal keeps it in `starting`; every other
 *                 error is terminal (restores the pre-#1383 catch-all's
 *                 coverage). Matches web's ProvisioningStepper terminal check.
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
 * FirstRunGate deliberately consumes only the `ready` one — the funnel must
 * not open behind a container that can still be taken over by the stepper's
 * cold-start promotion; see that file's docstring.
 */
import { useAuth } from "@clerk/clerk-expo";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AppState } from "react-native";
import {
  ApiError,
  classifyContainerStatus,
  deriveUIPhase,
  fetchJsonWithStatus,
  type ClassifiedStatus,
  type ContainerStatusBody,
  type StatusContract,
} from "@isol8/shared";

import { makeAdapter } from "./adapter";
import { isPresent } from "./presence";

export type ContainerStatus =
  | { kind: "loading" }
  | { kind: "ready"; onboarded?: boolean; teamsStatus?: string }
  | {
      kind: "starting";
      phase?: string;
      substatus?: string;
      onboarded?: boolean;
      /**
       * Top-level `teams_status` from the body (PR3 honest-contract), surfaced
       * on `ready`/`starting` alongside `onboarded` so ProvisionGate can render
       * the non-blocking Teams-degraded affordance. `"failed"` is the only
       * value acted on today; undefined on an old backend (never a false
       * alarm).
       */
      teamsStatus?: string;
    }
  | { kind: "parked" }
  /**
   * TERMINAL container failure — raw `status === "error"` with any substatus
   * OTHER than "error_retrying" (provision_failed, deployment_failed, a bare
   * error with no substatus, or any unknown value — PR3 honest-contract). Only
   * the explicit auto-retry signal ("error_retrying") stays `starting`;
   * everything else earns the blocking "Setup failed" interstitial. Keyed off
   * substatus, NEVER phase (a dead container still reports phase
   * "provisioning").
   */
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
 * Slower cadence for `blocked`. A gate can clear entirely server-side (an
 * admin completes the ChatGPT device-code flow on the web, credits land, a
 * subscription reactivates), and two long-lived INTERACTIVE screens sit on
 * this kind — ChatGPTConnectScreen's terminal "Connected — finishing up…"
 * state has no Retry at all and fires `onConnected` exactly once, so if that
 * single re-probe loses the race with the gate's eventually-consistent token
 * read, this poll is the only thing left that un-sticks the user. 30s rather
 * than 5s because nothing about a gate is urgent and nobody should hammer an
 * endpoint that dispatches provisioning work.
 */
const BLOCKED_POLL_INTERVAL_MS = 30_000;

/**
 * Poll cadence per status kind — `null` means "settled, stop polling".
 *
 * `error` polls at the SAME cadence as `starting` on purpose: this poll is
 * the backend's only auto-retry trigger for a failed container (see
 * routers/container.py, `retryable_states = ("error", "stopped")` — "This
 * poll is the only thing that runs again, so it re-dispatches"). Before
 * `error` existed as its own kind, a failed container classified as
 * `starting` and kept polling; giving it a surface without giving it a
 * cadence would mean the container never auto-recovers unless the user taps
 * Retry or backgrounds and foregrounds the app.
 *
 * `parked` polls because a foreground poll carries wake=1, so it drives the
 * box from asleep → starting → ready with no user action.
 *
 * `ready` / `loading` / `needsSignup` / `unreachable` don't poll: settled,
 * pre-first-response, or (for `unreachable`) recovered by the AppState
 * foreground re-check and the surfaces' own Retry.
 */
function pollIntervalFor(kind: ContainerStatus["kind"]): number | null {
  switch (kind) {
    case "starting":
    case "teamPending":
    case "parked":
    case "error":
      return POLL_INTERVAL_MS;
    case "blocked":
      return BLOCKED_POLL_INTERVAL_MS;
    default:
      return null;
  }
}

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
      // `teams_status` isn't a declared field on the shared body type (it rides
      // the index signature), so narrow it to a string before surfacing.
      const teamsStatus =
        typeof info.teams_status === "string" ? info.teams_status : undefined;

      // Honest failure states (PR3), checked BEFORE deriveUIPhase: the
      // backend reports phase/lifecycle="provisioning" for a failed
      // container too (terminal-ness is keyed off `substatus`, NEVER
      // `phase`/`lifecycle` — the landmine), and the ONE non-terminal error
      // shape ("error_retrying") needs its own substatus surfaced on the
      // `starting` kind, which the generic mapping below doesn't carry.
      if (info.status === "error") {
        if (info.substatus === "error_retrying") {
          return {
            kind: "starting",
            phase: info.phase,
            substatus: info.substatus,
            onboarded: info.onboarded,
            teamsStatus,
          };
        }
        return { kind: "error" };
      }

      // Every other 200 body is the composed StatusContract (additive wire
      // fields — see routers/container.py::_status_response). ONE shared
      // classifier (packages/shared/src/hooks/statusContract.ts) decides
      // the phase here instead of this file's own ladder (mobile
      // onboarding-parity: the byte-forked twin of web's ProvisioningStepper
      // useMemo, now retired in favor of one implementation both call).
      const derived = deriveUIPhase(info as unknown as StatusContract);
      switch (derived.phase) {
        case "parked":
          // Deliberately-asleep trial box (wake-gated). Distinct from a cold
          // start: it's healthy, one intent away from running, so it must NOT
          // render as a perpetual "starting…" spinner. Foregrounding already
          // fired a wake in parallel (gateway provider), so this is transient.
          return { kind: "parked" };
        case "ready":
          return { kind: "ready", onboarded: info.onboarded, teamsStatus };
        case "error":
          // Defensive: deriveUIPhase can only reach "error" via
          // lifecycle==="error", which the info.status==="error" branch
          // above already handled — kept for exhaustiveness, not a live path.
          return { kind: "error" };
        // "cold_start" (provisioning | starting) is the common case; the
        // pre-container states (blocked/provider_selection_required/
        // needs_signup/team_pending) are NOT reachable from a 200 body in
        // today's wiring (the gate raises 402/404/202 before a container
        // response is ever built) — fall through to "starting" defensively
        // rather than crash on a lifecycle this endpoint doesn't actually
        // emit at 200 yet.
        default:
          return {
            kind: "starting",
            phase: info.phase,
            substatus: info.substatus ?? undefined,
            onboarded: info.onboarded,
            teamsStatus,
          };
      }
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

  // Poll while unsettled, at the cadence `pollIntervalFor` picks. The dep is
  // `status.kind` and nothing else, so the interval is torn down + rebuilt
  // only on a state TRANSITION — never once per tick, even though the cadence
  // now varies by kind: every poll that lands on the same kind produces a new
  // status OBJECT but the same `status.kind` string, so this effect doesn't
  // re-run. It stops entirely once the status is settled.
  useEffect(() => {
    const intervalMs = pollIntervalFor(status.kind);
    if (intervalMs == null) return;
    const id = setInterval(() => void load(), intervalMs);
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
