/**
 * The composed `StatusContract` — the shared shape both web and mobile
 * render, never reconstruct (spec PR-5, COMPOSER). Mirrors the dict
 * `core.services.readiness_state.compute_status` returns on the backend
 * and that `routers/container.py::_status_response` flattens onto the
 * wire `GET /container/status` 200 body — keep the two in lockstep.
 *
 * `deriveUIPhase` is the ONE function that turns a `StatusContract` into a
 * UI-rendering decision. It replaces:
 *   - web's `ProvisioningStepper.tsx` hand-rolled `phase` `useMemo` ladder
 *     (readiness reconstructed from raw `container.status`/`substatus`/
 *     `phase`/`teams_status` fields)
 *   - mobile's `containerStatus.ts::fromClassified` "container" branch
 *     (the byte-forked twin of the same ladder)
 *
 * Both call sites now delegate the READINESS decision to this module and
 * keep only their own presentational concerns (elapsed-time sub-stepping,
 * platform-specific card copy) local (fixes L4 — "mobile never re-onboards"
 * — by construction: one shared implementation, no separate mobile latch to
 * fall behind).
 *
 * INV-6: `chatAvailable` is derived SOLELY from `runtimePhase`
 * ("provisioning" | "starting" | "ready") — never from `lifecycle`,
 * `funnel`, `bootstrap`, or `teamsState`. A degraded/awaiting-brief owner
 * (funnel not submitted, or the Teams seed failed) whose container is warm
 * still gets `chatAvailable: true` and `uiPhase: "ready"`.
 */

import type { BlockedPayload } from "./containerStatus";

/** Chat availability's SOLE input (INV-6). */
export type RuntimePhase = "provisioning" | "starting" | "ready";

/**
 * The composer's onboarding-inclusive lifecycle bucket. Broader than
 * `RuntimePhase` — folds in the pre-container gate states (signup/provider
 * selection/blocked) and the parked/error runtime refinements — but NEVER
 * consulted for chat availability (see `chatAvailable` above).
 */
export type Lifecycle =
  | "needs_signup"
  | "provider_selection_required"
  | "blocked"
  | "provisioning"
  | "starting"
  | "parked"
  | "error"
  | "ready";

export type FunnelState = "not_started" | "submitted" | "complete";
/**
 * `dispatched` is CLAIMED, `sent` is DELIVERED (OpenClaw acked the chat.send),
 * `delivered` is COMPLETED (Friday's run reached chat.final). Mirrors
 * `BootstrapState` in apps/backend/core/repositories/paperclip_repo.py — keep
 * the two in lockstep.
 */
export type BootstrapState = "not_started" | "dispatched" | "sent" | "delivered" | "failed";

export interface StatusContract {
  owner_id?: string;
  lifecycle: Lifecycle;
  runtime_phase: RuntimePhase;
  chat_available: boolean;
  container_status: string | null;
  container_substatus: string | null;
  parked: boolean;
  onboarded: boolean;
  last_error: string | null;
  last_error_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  /** AWS region the container runs in (`settings.AWS_REGION`) — always set,
   * never derived from any of the other composed inputs. */
  region: string;
  generations: {
    provision_generation: string | null;
    onboarding_generation: string | null;
  };
  funnel: {
    state: FunnelState;
    goal_id: string | null;
    project_id: string | null;
    submitted_at: string | null;
    completed_at: string | null;
  };
  bootstrap: {
    state: BootstrapState;
    dispatch_id?: string | null;
    dispatched_at: string | null;
    delivered_at: string | null;
    attempts: number;
    last_error: string | null;
  };
  provider: {
    choice: string | null;
    byo_provider: string | null;
    is_org: boolean;
    subscription_status?: string | null;
  };
  teams_state: string | null;
  // Tolerates additive backend fields without narrowing every unlisted
  // field to `unknown` project-wide. No legacy flat duplicates exist on
  // the wire anymore (CLIENT-HOOK deleted `phase`/`status`/`substatus`/
  // `teams_status` from `_status_response` — the contract fields above are
  // the ONLY wire shape) — do not reintroduce a fallback read of them here.
  [k: string]: unknown;
}

/**
 * The ONE discriminated UI phase every renderer (web's stepper, mobile's
 * ProvisioningStepper/ProvisionGate) switches on. Deliberately coarser
 * than `Lifecycle` — "provisioning" and "starting" both collapse into
 * "cold_start" here because splitting them further (web's
 * gateway/channels-sidecars/agent sub-steps, driven by elapsed time) is a
 * presentational concern each platform owns locally, not a readiness
 * decision. `runtimePhase`/`lifecycle` stay on the result for exactly that
 * local sub-stepping.
 */
export type UIPhase =
  | "blocked"
  | "provider_selection_required"
  | "needs_signup"
  | "team_pending"
  | "cold_start"
  | "error"
  | "parked"
  | "ready";

export interface DerivedUIPhase {
  phase: UIPhase;
  /** SOLE input to chat availability (INV-6) — mirrors `contract.runtime_phase`. */
  runtimePhase: RuntimePhase;
  /** SOLE input to `phase === "ready"` — mirrors `contract.chat_available`. */
  chatAvailable: boolean;
  /** Raw composer lifecycle, for a renderer that needs finer detail than `phase`. */
  lifecycle: Lifecycle;
  /** True when the Paperclip company seed failed — a NON-BLOCKING degraded
   *  signal that rides alongside a `ready`/`cold_start` phase, never its own
   *  phase (INV-6: onboarding-adjacent facts never gate chat). */
  teamsDegraded: boolean;
  funnel: StatusContract["funnel"];
  bootstrap: StatusContract["bootstrap"];
  generations: StatusContract["generations"];
}

const RUNTIME_PHASES: readonly RuntimePhase[] = ["provisioning", "starting", "ready"];

/**
 * Resolve `runtime_phase`, falling back to the legacy flat `phase` field
 * when the rich contract field is absent. The two are semantically
 * IDENTICAL in every real response (`routers/container.py::_status_response`
 * sets `"phase": contract.get("runtime_phase")` verbatim), so this is a
 * safe, meaningful fallback — not a guess — covering: a minimal/legacy-
 * shaped body (older backend during rolling-deploy skew) and test doubles
 * that only set the flat field. `phase: "parked"` is NOT a `RuntimePhase`
 * value (it's a `Lifecycle` refinement on top of "provisioning" — a parked
 * box has no live runtime, hence not "ready") so it's excluded here and
 * handled by `resolveLifecycle` below instead.
 */
function resolveRuntimePhase(contract: StatusContract): RuntimePhase {
  if (contract.runtime_phase) return contract.runtime_phase;
  const legacy = contract.phase;
  if (typeof legacy === "string" && (RUNTIME_PHASES as readonly string[]).includes(legacy)) {
    return legacy as RuntimePhase;
  }
  // Both `runtime_phase` AND the legacy `phase` are genuinely ABSENT
  // (not merely a non-runtime value like "parked") — the pre-phase-field
  // backend convention this preserves: a bare container object with no
  // phase at all defaults to "ready", not "provisioning". A present-but-
  // unrecognized value (e.g. "parked", handled by resolveLifecycle
  // instead) falls through to the conservative "provisioning" default.
  if (legacy == null) return "ready";
  return "provisioning";
}

function resolveChatAvailable(contract: StatusContract, runtimePhase: RuntimePhase): boolean {
  if (typeof contract.chat_available === "boolean") return contract.chat_available;
  return runtimePhase === "ready"; // INV-6: SOLELY runtime_phase.
}

function resolveLifecycle(contract: StatusContract, runtimePhase: RuntimePhase): Lifecycle {
  if (contract.lifecycle) return contract.lifecycle;
  if (contract.phase === "parked" || contract.parked) return "parked";
  if (contract.container_status === "error" || contract.status === "error") return "error";
  return runtimePhase;
}

function resolveTeamsState(contract: StatusContract): string | null {
  if (contract.teams_state !== undefined) return contract.teams_state;
  const legacy = contract.teams_status;
  return typeof legacy === "string" ? legacy : null;
}

const EMPTY_FUNNEL: StatusContract["funnel"] = {
  state: "not_started",
  goal_id: null,
  project_id: null,
  submitted_at: null,
  completed_at: null,
};

const EMPTY_BOOTSTRAP: StatusContract["bootstrap"] = {
  state: "not_started",
  dispatch_id: null,
  dispatched_at: null,
  delivered_at: null,
  attempts: 0,
  last_error: null,
};

const EMPTY_GENERATIONS: StatusContract["generations"] = {
  provision_generation: null,
  onboarding_generation: null,
};

/**
 * `team_pending` is a CALLER-ROLE fact (org member vs admin), not something
 * the owner-keyed `StatusContract` alone can express — the backend composer
 * takes only `owner_id`, and a member polling about an admin's not-yet-
 * provisioned container looks identical, from the owner's own contract, to
 * "not provisioned yet". The web/mobile HTTP classifiers already carry this
 * as a distinct 202 status-code branch (`ClassifiedStatus.kind ===
 * "team-pending"` in `containerStatus.ts`) BEFORE a `StatusContract` body
 * ever exists for that request. So `deriveUIPhase` takes an optional
 * `isTeamPending` flag callers set from that 202 branch — default false,
 * the overwhelmingly common case (a body always accompanies a normal 200).
 */
export function deriveUIPhase(
  contract: StatusContract,
  options: { isTeamPending?: boolean } = {},
): DerivedUIPhase {
  const runtimePhase = resolveRuntimePhase(contract);
  const chatAvailable = resolveChatAvailable(contract, runtimePhase);
  const lifecycle = resolveLifecycle(contract, runtimePhase);
  const teamsState = resolveTeamsState(contract);
  const teamsDegraded = teamsState === "failed";

  let phase: UIPhase;
  if (options.isTeamPending) {
    phase = "team_pending";
  } else {
    switch (lifecycle) {
      case "needs_signup":
        phase = "needs_signup";
        break;
      case "provider_selection_required":
        phase = "provider_selection_required";
        break;
      case "blocked":
        phase = "blocked";
        break;
      case "error":
        phase = "error";
        break;
      case "parked":
        phase = "parked";
        break;
      case "ready":
        phase = "ready";
        break;
      case "provisioning":
      case "starting":
      default:
        phase = "cold_start";
        break;
    }
  }

  return {
    phase,
    runtimePhase,
    chatAvailable,
    lifecycle,
    teamsDegraded,
    funnel: contract.funnel ?? EMPTY_FUNNEL,
    bootstrap: contract.bootstrap ?? EMPTY_BOOTSTRAP,
    generations: contract.generations ?? EMPTY_GENERATIONS,
  };
}

export type { BlockedPayload };
