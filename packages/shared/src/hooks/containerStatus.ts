/**
 * `GET /container/status` classifier — the pure status-code + payload-shape
 * parse shared by web (`useProvisioningState.ts`) and mobile
 * (`containerStatus.ts`). Extracted because the two were independently
 * hand-written copies of the same parse and had already drifted (mobile's
 * copy silently dropped the top-level `blocked` fallback and never surfaced
 * a "container failed" state) — mobile onboarding-parity Slice 1.
 *
 * Deliberately NOT here: readiness / polling / rendering decisions. The 200
 * body is passed through UNTOUCHED — `runtime_phase`, `lifecycle`,
 * `container_status`, `onboarded`, etc. are each platform's own binding's
 * job to interpret (e.g. `lifecycle === "parked"` is ready-equivalent on
 * web's stepper but "keep polling, show a banner" on mobile — a
 * rendering/polling call, not a parse). This module never fetches and never
 * throws: callers pass the SUCCESS `{status, body}` from `fetchJsonWithStatus`
 * for 2xx responses, and `{status: err.status, body: err.body}` (from the
 * `ApiError` they caught) for non-2xx — both funnel through the same
 * status-code table below.
 */

import { useSyncExternalStore } from "react";

/**
 * Which runtime hosts this workspace — `containers.runtime_host`, via
 * `core/services/runtime_transport.host_for`, which fails closed to `"ecs"`.
 * A workspace moves ECS -> AgentCore once, one way, and never back.
 */
export type RuntimeHost = "ecs" | "agentcore";

/**
 * The 200 body — the composed `StatusContract` (see `statusContract.ts` and
 * `core/services/readiness_state.compute_status`/
 * `routers/container.py::_status_response`). Two shapes exist on the wire
 * (both handled identically here — the classifier doesn't care which): the
 * normal container row and a synthetic "provisioning just kicked off" body
 * for a fresh owner with no container row yet (`container_substatus:
 * "ensure_user_ready"`). No legacy flat duplicates (`phase`/`status`/
 * `substatus`/`teams_status`) — CLIENT-HOOK deleted those from
 * `_status_response`; there are no users and no backward-compat
 * requirement.
 */
export interface ContainerStatusBody {
  service_name: string | null;
  container_status: string | null;
  container_substatus: string | null;
  /** Composer's runtime phase — see `readiness_state.resolve_runtime_phase`. */
  runtime_phase?: "provisioning" | "starting" | "ready";
  /** Composer's onboarding-inclusive lifecycle bucket (broader than
   * `runtime_phase` — folds in parked/error/pre-container states). */
  lifecycle?: string;
  /** Per-container onboarding fact (funnel `state === "complete"`, or the
   * legacy `onboarded_at` fallback) — an org shares one container. */
  onboarded?: boolean;
  /** Set alongside `lifecycle: "parked"` on a deliberately-asleep, wake-gated trial box. */
  parked?: boolean;
  /** Runtime capability signal — absent on a backend older than M6 task 10, which is
   * indistinguishable from `"ecs"` and must be treated as such. */
  runtime_host?: RuntimeHost;
  /** Non-blocking Paperclip (Teams) seed health — `"failed"` is the only
   * value any binding acts on today. */
  teams_state?: string | null;
  last_error?: string | null;
  // Tolerates both 200 shapes plus any field a caller reads that isn't
  // named above, without narrowing it to `unknown` project-wide.
  [k: string]: unknown;
}

/** The 402 gate payload — `Gate.to_payload()` in `core/services/provision_gate.py`. */
export interface BlockedPayload {
  code: string;
  title: string;
  message: string;
  action: {
    kind: string;
    label: string;
    href: string;
    admin_only: boolean;
  };
  owner_role: "admin" | "member";
}

export type ClassifiedStatus =
  | { kind: "container"; body: ContainerStatusBody } // 200 — RAW body passed through untouched
  | { kind: "team-pending"; detail?: string } // 202 — org member, admin's workspace not yet provisioned
  | { kind: "needs-signup" } // 404 — admin has never started signup
  | { kind: "blocked"; blocked: BlockedPayload } // 402 — provision gate fired
  | { kind: "unrecognized"; status: number }; // any other status — caller decides (web throws, mobile -> unreachable)

/**
 * Reads a 402 body's blocked payload. FastAPI's `HTTPException(detail=...)`
 * serializes `gate.to_payload()` (`{"blocked": {...}}`) to the wire as
 * `{"detail": {"blocked": {...}}}`, so this reads `body.detail.blocked`
 * first; falls back to a top-level `body.blocked` for resilience against
 * test fixtures / transport quirks that skip the `detail` envelope. Returns
 * `null` when neither shape is present — callers must NOT synthesize a fake
 * blocked screen in that case.
 */
export function parseBlockedPayload(body: unknown): BlockedPayload | null {
  const envelope =
    body && typeof body === "object" ? (body as Record<string, unknown>) : {};
  const detail = envelope.detail;
  const blocked =
    detail && typeof detail === "object" && "blocked" in (detail as object)
      ? (detail as { blocked: BlockedPayload }).blocked
      : (envelope.blocked as BlockedPayload | undefined);
  return blocked ?? null;
}

/**
 * Classifies a `/container/status` response by HTTP status code + payload
 * shape:
 *
 *   200 -> container    (body passed through RAW — see `ContainerStatusBody`)
 *   202 -> team-pending
 *   404 -> needs-signup
 *   402 -> blocked        (if `parseBlockedPayload` finds a payload)
 *        | unrecognized   (if it doesn't — defensive; the backend always
 *                           sends one in practice)
 *   *   -> unrecognized
 *
 * Never throws, never fetches. `unrecognized` is a deliberate non-decision:
 * each binding keeps its own existing policy for it (web throws, surfacing
 * a real SWR error instead of a silently stuck UI; mobile maps it to
 * `{kind: "unreachable"}`) — that policy lives in the binding, not here.
 */
export function classifyContainerStatus(res: {
  status: number;
  body: unknown;
}): ClassifiedStatus {
  const { status, body } = res;

  if (status === 200) {
    return { kind: "container", body: body as ContainerStatusBody };
  }

  if (status === 202) {
    // Backend Task 8: org members whose admin-owned workspace hasn't been
    // provisioned yet get 202 with a {"detail": "..."} message.
    const detail =
      body &&
      typeof body === "object" &&
      typeof (body as Record<string, unknown>).detail === "string"
        ? ((body as Record<string, unknown>).detail as string)
        : undefined;
    return { kind: "team-pending", detail };
  }

  if (status === 404) {
    // "Owner has never started signup": the gate would fire
    // `signup_required` for an admin, but /container/status converts that
    // to 404 so the chat surface can render the provider picker instead of
    // trapping the user in a Subscribe-loop UI.
    return { kind: "needs-signup" };
  }

  if (status === 402) {
    const blocked = parseBlockedPayload(body);
    // Defensive: a 402 without a recognizable blocked payload isn't a state
    // this classifier can represent — fall through to unrecognized rather
    // than synthesizing an empty blocked screen.
    if (!blocked) return { kind: "unrecognized", status };
    return { kind: "blocked", blocked };
  }

  return { kind: "unrecognized", status };
}

/* ---------------------------------------------------------------------------
 * The runtime capability signal (M6 task 10, row 0)
 *
 * `runtime_host` arrives on the `/container/status` body, but the surfaces that
 * need it (control panels, deep leaf components) do not hold that body and must
 * not open a second poll to get it. So the ONE reader of that endpoint —
 * `useOnboardingStatus` — publishes the field here and `useRuntimeHost()` is a
 * parameterless subscriber. Module state + a listener set + `useSyncExternalStore`
 * is the pattern `workspaceController.ts` and `lampStore.ts` already use.
 *
 * **The value is SCOPED, and that is the whole safety property.** A workspace
 * switch is client-side (`setActive` + `router.replace`, never a reload), so this
 * module — and anything cached in it — survives it. An unscoped singleton would
 * keep serving the PREVIOUS workspace's runtime until the new one's status fetch
 * resolved, and a panel reading it would open ECS-only surfaces on an AgentCore
 * workspace (or fire an RPC that wakes and bills a microVM). So the reader
 * declares which workspace it is polling (`setRuntimeHostScope`, during render,
 * in the same pass that clears its own sticky body), the publish carries that
 * same scope, and a host from any other scope reads as unknown.
 *
 * `undefined` therefore means NOT KNOWN *FOR THIS WORKSPACE* — no status has
 * landed yet, a switch is in flight, the poll is disabled, or the backend is
 * older than this change. Every caller must treat it as "hold", never as "this is
 * ECS": the two states differ precisely when guessing wrong is expensive. A
 * caller that ALREADY holds the status body should read `body.runtime_host`
 * directly instead of subscribing here — it gets the answer in the same render
 * with no unknown window at all (`useSystemHealth` does exactly that).
 * ------------------------------------------------------------------------- */

/** The status key the one reader is currently polling — see `setRuntimeHostScope`. */
let activeStatusScope: string | null = null;
let publishedHost: { scope: string; host: RuntimeHost } | null = null;
const runtimeHostListeners = new Set<() => void>();

function notifyRuntimeHost(): void {
  for (const listener of runtimeHostListeners) listener();
}

/**
 * Declare which workspace the status poll is currently for. Called by
 * `useOnboardingStatus` DURING RENDER, alongside the branch that drops its own sticky
 * last-good body on a key change, so the host goes unknown in the same pass the body does —
 * an effect would leave one commit in which a panel still sees the old workspace's runtime.
 *
 * Deliberately does NOT notify: React forbids updating another component from a render phase,
 * and `useSyncExternalStore` re-reads the snapshot every render and re-checks it after commit,
 * so a plain mutation is picked up without one. `null` (the poll is disabled — signed out)
 * matches no published scope, so it reads as unknown.
 */
export function setRuntimeHostScope(scope: string | null): void {
  activeStatusScope = scope;
}

/** Called by `useOnboardingStatus` when a status body for `scope` names a host. Never
 * publishes `undefined`: within one workspace, a failing or disabled poll must not blank a
 * host we already know — going unknown is `setRuntimeHostScope`'s job, and only on a switch. */
export function publishRuntimeHost(scope: string, host: RuntimeHost): void {
  if (publishedHost?.scope === scope && publishedHost.host === host) return;
  publishedHost = { scope, host };
  notifyRuntimeHost();
}

function subscribeRuntimeHost(listener: () => void): () => void {
  runtimeHostListeners.add(listener);
  return () => {
    runtimeHostListeners.delete(listener);
  };
}

function readRuntimeHost(): RuntimeHost | undefined {
  return publishedHost && publishedHost.scope === activeStatusScope ? publishedHost.host : undefined;
}

/** Test-only reset — this module's state outlives any one test's renders. */
export function __resetRuntimeHost(): void {
  activeStatusScope = null;
  publishedHost = null;
  runtimeHostListeners.clear();
}

/** This workspace's runtime, or `undefined` while unknown (see the block comment above). */
export function useRuntimeHost(): RuntimeHost | undefined {
  return useSyncExternalStore(subscribeRuntimeHost, readRuntimeHost, readRuntimeHost);
}

/* ---------------------------------------------------------------------------
 * M6 task 10-D (owner ruling M6-R50): surfaces with no Hermes/AgentCore
 * equivalent at all -- heartbeat, plugins, dreams, the container browser,
 * channel status/wiring, Shopify's container-side login, Brain. Each of those
 * call sites needs the exact same three-way answer `useRuntimeHost()` gives,
 * so this is the one place that spells it out instead of ten call sites each
 * re-deriving "agentcore" vs. "not yet known" by hand.
 * ------------------------------------------------------------------------- */

/**
 * "loading" -- the host for THIS workspace isn't known yet; render whatever the
 *   surface renders today for a loading state, and hold its RPC exactly as
 *   `useRuntimeHost()`'s own contract requires (never fire on a guess).
 * "hidden" -- confirmed AgentCore; the surface has no equivalent and must not render.
 * "shown" -- ECS (or, transitionally, any host other than "agentcore"); unchanged.
 */
export type AgentCoreGate = "loading" | "hidden" | "shown";

/** Tri-state gate for a control-panel surface that is deleted outright on AgentCore. */
export function useHiddenOnAgentCore(): AgentCoreGate {
  const host = useRuntimeHost();
  if (host === undefined) return "loading";
  return host === "agentcore" ? "hidden" : "shown";
}
