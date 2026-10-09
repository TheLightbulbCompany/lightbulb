/**
 * Provision-gate / onboarding readiness probe (W5 contract §B).
 *
 * `GET /container/status` encodes state in the HTTP STATUS CODE, not just the
 * body — `useOnboardingStatus`'s fetcher reads it via `fetchJsonWithStatus`
 * (200 vs 202) and catches `ApiError` for 402 (blocked gate) and 404
 * (needs-signup) internally. The status-code + payload-shape parse itself
 * lives in `@lightbulb/shared`'s `classifyContainerStatus`
 * (mobile onboarding-parity Slice 1) — this file's job is ONLY turning a
 * `ClassifiedStatus` into this file's richer `ContainerStatus` (which
 * additionally distinguishes ready / starting / parked / error from the 200
 * body's `container_status`/`runtime_phase` fields — a rendering decision the
 * shared classifier deliberately leaves to each platform).
 *
 * The React binding (fetch + AppState foreground refetch) is no longer
 * hand-rolled here — it's the ONE shared `useOnboardingStatus`
 * hook (spec PR-9, CLIENT-HOOK; `packages/shared/src/hooks/
 * useOnboardingStatus.ts`), the same hook web's `useProvisioningState`
 * delegates to. This is what makes mobile re-onboarding parity-by-
 * construction (fixes L4 — "mobile never re-onboards" — per the spec: "one
 * shared implementation, no separate mobile latch to fall behind") rather
 * than a second hand-ported copy that can silently drift, which is exactly
 * how the pre-COMPOSER mobile classifier drifted from web's (dropped the
 * top-level `blocked` fallback, never surfaced a "container failed" state —
 * see this file's original mobile onboarding-parity Slice 1 motivation).
 * There is no polling: the status is fetched on mount and on foreground
 * (AppState "active"), and refetched by `providers/live-events.tsx` on a
 * `runtime.resource.invalidated{resource:"onboarding"}` event — see the
 * shared hook's module doc.
 *
 * Classification (contract §B1/§B3):
 *
 *   ready       — 200 with phase === "ready" (openclaw responsive)
 *   starting    — 200 with phase !== "ready" (cold start / provisioning), OR
 *                 the ONE non-terminal error shape: status === "error" with
 *                 substatus === "error_retrying" (backend is auto-retrying)
 *   parked      — 200 with phase === "parked" (deliberately-asleep trial box)
 *   error       — 200 with raw status === "error" and ANY substatus other than
 *                 "error_retrying" — provision_failed, deployment_failed, a
 *                 bare error with no substatus
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
 *   workspaceRequired — 409 `workspace_required`: the session carries no org
 *                 claim (the only 409 `/container/status` raises). BLOCKS, and
 *                 routes to the workspace chooser — as `unreachable` it fell
 *                 through to Home, which dead-ended on Retry (F4).
 *   unreachable — network / auth / unexpected error, OR an unrecognized
 *                 status code the shared classifier couldn't place
 *
 * getTokenRef REST pattern (control/overview.tsx lesson): pin Clerk's churning
 * `getToken` behind a ref so the adapter config is built exactly once —
 * `useOnboardingStatus` itself never sees `getToken`'s churn.
 *
 * `ready` (and `starting`, when the body carries one) also preserves the
 * per-CONTAINER `onboarded` fact for status consumers and legacy responses.
 * `undefined` means "not known" (old backend / not-yet-ready).
 */
import { useAuth } from "@clerk/expo";
import { useMemo, useRef } from "react";
import { AppState } from "react-native";
import {
  deriveUIPhase,
  useOnboardingStatus,
  type ClassifiedStatus,
  type ContainerStatusBody,
  type StatusContract,
  type VisibilityAdapter,
} from "@lightbulb/shared";

import { makeAdapter } from "./adapter";

export type ContainerStatus =
  | { kind: "loading" }
  | {
      kind: "ready";
      onboarded?: boolean;
      teamsStatus?: string;
    }
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
  | { kind: "workspaceRequired" }
  | { kind: "unreachable"; message: string };

/**
 * Turns the shared classifier's status-code-level result into this file's
 * richer `ContainerStatus`. `unreachableMessage` is only used for the
 * `unrecognized` branch — a generic fallback, since `useOnboardingStatus`'s
 * rejection-safe fetcher reclassifies a caught `ApiError` internally without
 * surfacing its original message text (see that hook's module doc).
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
      // DEFENSIVE ONLY: `useOnboardingStatus` stopped letting an
      // `unrecognized` outcome become the classified value (it reports via
      // `networkError` instead), and `useContainerStatus` now checks
      // `networkError` first anyway — so a 5xx reaches "unreachable" by that
      // route, carrying the real status code in its message rather than this
      // generic fallback. Kept as a total-switch guard.
      return { kind: "unreachable", message: unreachableMessage };
    case "container": {
      // Defensive against an empty/malformed 200 body — mirrors this file's
      // pre-Slice-1 behavior. The classifier's `ContainerStatusBody` type
      // isn't nullable, but that's a type-level claim, not a runtime one.
      const info = classified.body ?? ({} as ContainerStatusBody);
      // `teams_state` isn't a declared field on the shared body type (it
      // rides the index signature), so narrow it to a string before
      // surfacing. No legacy `teams_status` fallback (spec PR-9,
      // CLIENT-HOOK deleted the flat duplicate from `_status_response` —
      // there are no users and no backward-compat requirement).
      const teamsStatus =
        typeof info.teams_state === "string" ? info.teams_state : undefined;

      // Honest failure states (PR3), checked BEFORE deriveUIPhase: the
      // backend reports runtime_phase/lifecycle="provisioning" for a failed
      // container too (terminal-ness is keyed off `container_substatus`,
      // NEVER `runtime_phase`/`lifecycle` — the landmine), and the ONE
      // non-terminal error shape ("error_retrying") needs its own substatus
      // surfaced on the `starting` kind, which the generic mapping below
      // doesn't carry.
      if (info.container_status === "error") {
        if (info.container_substatus === "error_retrying") {
          return {
            kind: "starting",
            phase: info.runtime_phase,
            substatus: info.container_substatus,
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
          return {
            kind: "ready",
            onboarded: info.onboarded,
            teamsStatus,
          };
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
            phase: info.runtime_phase,
            substatus: info.container_substatus ?? undefined,
            onboarded: info.onboarded,
            teamsStatus,
          };
      }
    }
  }
}

/** Mobile's `VisibilityAdapter` for `useOnboardingStatus` — wraps React
 * Native's `AppState` the way web's `webVisibilityAdapter` wraps
 * `document.visibilityState`. "Active" is mobile's one true presence signal
 * (see `presence.ts`'s module doc: iOS never brings an app to the
 * foreground on its own, so there is no "visible but unattended" state to
 * defend against the way there is on desktop occlusion-throttling). */
const mobileVisibilityAdapter: VisibilityAdapter = {
  isVisible: () => AppState.currentState === "active",
  subscribe: (onChange) => {
    const sub = AppState.addEventListener("change", () => onChange());
    return () => sub.remove();
  },
};

export function useContainerStatus() {
  const { getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const config = useMemo(() => makeAdapter(() => getTokenRef.current()), []);

  // The ONE shared status read (spec PR-9, CLIENT-HOOK) — same hook web's
  // `useProvisioningState` delegates to.
  const { classified, networkError, refresh } = useOnboardingStatus({
    api: config,
    enabled: true,
    visibility: mobileVisibilityAdapter,
  });

  // A LIVE failure outranks the sticky last-good body. `useOnboardingStatus`
  // keeps the last successfully classified status across a failed fetch.
  // That stickiness broke the old `classified ? … : networkError ? …`
  // order here: once ANY good fetch had
  // landed, `classified` stayed truthy for the rest of the session and the
  // `networkError` branch became unreachable, so a sustained outage rendered
  // a frozen cold-start screen with no way out.
  //
  // Checking `networkError` FIRST restores this binding's pre-existing
  // semantics exactly (before stickiness, a failed fetch nulled `classified`,
  // so a failure always landed here). That is the right policy for MOBILE
  // specifically: `unreachable` is NON-BLOCKING here — ProvisionGate falls
  // through to `children` — so it degrades to the app rather than trapping
  // the user. Rendering policy belongs to each binding; web can retain the
  // sticky last-good read while mobile surfaces current connectivity loss.
  const status: ContainerStatus = networkError
    ? (networkError as Error & { status?: number }).status === 409
      ? { kind: "workspaceRequired" }
      : { kind: "unreachable", message: networkError.message }
    : classified
      ? fromClassified(classified, "Request failed")
      : { kind: "loading" };

  return { status, refetch: refresh };
}
