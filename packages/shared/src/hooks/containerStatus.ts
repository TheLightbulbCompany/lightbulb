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
 * The "status loaded" signal
 *
 * Runtime RPC surfaces (control panels, deep leaf components) do not hold the
 * `/container/status` body and must not open a second poll to get it. So the ONE
 * reader of that endpoint — `useOnboardingStatus` — publishes "a 200 body has
 * landed for this workspace" here and `useStatusLoaded()` is a parameterless
 * subscriber. Module state + a listener set + `useSyncExternalStore` is the
 * pattern `workspaceController.ts` and `lampStore.ts` already use.
 *
 * `false` means NOT KNOWN *FOR THIS WORKSPACE* — no status has landed yet, a
 * switch is in flight, the poll is disabled, or this owner has no runtime at all
 * (a fresh, blocked or parked workspace answers 402 / 404, so nothing
 * publishes). Every caller must treat it as "hold": firing a runtime RPC on a
 * guess wakes and bills a microVM (~55s cold start).
 *
 * **The value is SCOPED**, so a client-side workspace switch (`setActive` +
 * `router.replace`, never a reload) does not leave the previous workspace's
 * answer standing: the reader declares which workspace it is polling
 * (`setStatusScope`, during render, in the same pass that clears its own sticky
 * body), the publish carries that same scope, and a publish from any other
 * scope reads as not loaded.
 * ------------------------------------------------------------------------- */

/** The status key the one reader is currently polling — see `setStatusScope`. */
let activeStatusScope: string | null = null;
let loadedScope: string | null = null;
const statusLoadedListeners = new Set<() => void>();

/**
 * Declare which workspace the status poll is currently for. Called by
 * `useOnboardingStatus` DURING RENDER, alongside the branch that drops its own sticky
 * last-good body on a key change, so the signal goes false in the same pass the body does —
 * an effect would leave one commit in which a panel still sees the old workspace's runtime.
 *
 * Deliberately does NOT notify: React forbids updating another component from a render phase,
 * and `useSyncExternalStore` re-reads the snapshot every render and re-checks it after commit,
 * so a plain mutation is picked up without one. `null` (the poll is disabled — signed out)
 * matches no published scope, so it reads as not loaded.
 */
export function setStatusScope(scope: string | null): void {
  activeStatusScope = scope;
}

/** Called by `useOnboardingStatus` when a 200 status body lands for `scope`. There is no
 * unpublish: within one workspace, a failing or disabled poll must not blank a status we
 * already had — going false is `setStatusScope`'s job, and only on a switch. */
export function publishStatusLoaded(scope: string): void {
  if (loadedScope === scope) return;
  loadedScope = scope;
  for (const listener of statusLoadedListeners) listener();
}

function subscribeStatusLoaded(listener: () => void): () => void {
  statusLoadedListeners.add(listener);
  return () => {
    statusLoadedListeners.delete(listener);
  };
}

function readStatusLoaded(): boolean {
  return loadedScope !== null && loadedScope === activeStatusScope;
}

/** Test-only reset — this module's state outlives any one test's renders. */
export function __resetStatusLoaded(): void {
  activeStatusScope = null;
  loadedScope = null;
  statusLoadedListeners.clear();
}

/** Whether a status body has landed for this workspace (see the block comment above). */
export function useStatusLoaded(): boolean {
  return useSyncExternalStore(subscribeStatusLoaded, readStatusLoaded, readStatusLoaded);
}
