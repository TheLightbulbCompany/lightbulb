import { useRef } from "react";

/**
 * How long a surface may present a retryable failure as "warming up" before it
 * must show the user the error and a way out.
 *
 * Sized for a genuine cold start end-to-end: ECS pulling the image, the task
 * publishing an IP, and the OpenClaw sidecar completing its protocol-v4
 * handshake — comfortably under two minutes in practice.
 */
export const WARMING_GRACE_MS = 90_000;

/**
 * Bounds how long a `retryable` failure is allowed to read as loading.
 *
 * `retryable` is a HINT FROM THE RESPONDER, NOT A LIVENESS PROOF, and treating
 * it as terminal is a trap this codebase already fell into: the backend flags
 * "container has no IP yet" retryable, but that same condition covers a task
 * that is still pulling its image AND one that has died and is being replaced
 * (see resolve_running_container's docstring). A client that suppresses the
 * error card for the whole retryable class therefore renders a crashlooping or
 * cap-killed container as an endless spinner with no message and no escape —
 * strictly worse than the scary-but-honest card it replaced.
 *
 * So: believe the hint, but only for a bounded window. After it, the failure
 * is presented as a failure regardless of what the responder claimed.
 *
 * No timer: every caller is driven by a retry loop that re-renders (SWR's fixed
 * 3s error-retry cadence, or the history screen's own scheduled refetch), so
 * the deadline is evaluated at render. A surface with NO retry loop behind it
 * must not use this hook — it would sit past the deadline until something else
 * re-rendered it.
 */
export function useWarmingWindow(
  isRetryableFailure: boolean,
  graceMs: number = WARMING_GRACE_MS,
): boolean {
  const startedAtRef = useRef<number | null>(null);
  if (!isRetryableFailure) {
    startedAtRef.current = null;
    return false;
  }
  if (startedAtRef.current === null) startedAtRef.current = Date.now();
  return Date.now() - startedAtRef.current < graceMs;
}
