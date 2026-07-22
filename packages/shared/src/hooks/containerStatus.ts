/**
 * `GET /container/status` classifier — the pure status-code + payload-shape
 * parse shared by web (`useProvisioningState.ts`) and mobile
 * (`containerStatus.ts`). Extracted because the two were independently
 * hand-written copies of the same parse and had already drifted (mobile's
 * copy silently dropped the top-level `blocked` fallback and never surfaced
 * a "container failed" state) — mobile onboarding-parity Slice 1. See
 * docs/superpowers/plans/2026-07-21-mobile-onboarding-parity.md.
 *
 * Deliberately NOT here: readiness / polling / rendering decisions. The 200
 * body is passed through UNTOUCHED — `phase`, `status`, `onboarded`, etc.
 * are each platform's own binding's job to interpret (e.g. `phase ===
 * "parked"` is ready-equivalent on web's stepper but "keep polling, show a
 * banner" on mobile — a rendering/polling call, not a parse). This module
 * never fetches and never throws: callers pass the SUCCESS `{status, body}`
 * from `fetchJsonWithStatus` for 2xx responses, and `{status: err.status,
 * body: err.body}` (from the `ApiError` they caught) for non-2xx — both
 * funnel through the same status-code table below.
 */

/**
 * The 200 body. Two shapes exist on the wire (both handled identically
 * here — the classifier doesn't care which): the normal container row
 * (`_status_response` in `routers/container.py`) and a synthetic
 * "provisioning just kicked off" body for a fresh owner with no container
 * row yet (same file, `substatus: "ensure_user_ready"`). Optional fields
 * default to `undefined` (never a false negative) on an old backend that
 * predates them.
 */
export interface ContainerStatusBody {
  service_name: string | null;
  status: string | null;
  substatus: string | null;
  /** Cold-start phase — see `_resolve_cold_start_phase` in `routers/container.py`. */
  phase?: "provisioning" | "starting" | "ready" | "parked";
  /** Per-container onboarding fact (`container.onboarded_at is not None`) — an org shares one container. */
  onboarded?: boolean;
  /** Set alongside `phase: "parked"` on a deliberately-asleep, wake-gated trial box. */
  parked?: boolean;
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
