import type { ContainerStatus } from "./containerStatus";

/**
 * Whether a roster surface should present a failed/absent agents.list as
 * "still coming up" rather than as a failure.
 *
 * The answer comes from `/container/status` — the server-truth contract
 * ProvisionGate already polls and publishes through ContainerStatusContext —
 * NOT from the shape of the RPC error.
 *
 * This started life as a 90s timer (`useWarmingWindow`) bounding how long we
 * would believe a `retryable` flag, and that was wrong twice over. It was
 * inferring container liveness from error strings when the app already asks
 * the server directly; and the bound itself was per-uninterrupted-observation
 * (its deadline lived in a component ref that reset on every park/unpark and
 * remount), so a dead container re-armed the grace forever — the exact
 * unbounded spinner it existed to prevent.
 *
 * Reading the status contract removes the guess entirely:
 *
 *  - No arbitrary constant. The wait lasts exactly as long as the server says
 *    the container is coming up — which matters because this repo's own bound
 *    is minutes, not seconds (SPOT_PLACEMENT_FALLBACK_S is 300s, and
 *    `_await_running_transition` documents "10s or 10 minutes").
 *  - A dead container is handled BETTER, not merely bounded: status flips to
 *    `error` and ProvisionGate replaces the whole tree with its blocking
 *    "Setup failed" interstitial.
 *  - A ready container whose RPC fails reports immediately instead of
 *    pretending to wake.
 */
export function isContainerComingUp(status: ContainerStatus | null): boolean {
  // `null` = ProvisionGate has not published yet (no provider in a unit test,
  // or the very first paint). Treat as coming-up: it is the state we are in
  // before the first poll answers, and calling a failure "real" on the
  // strength of not having asked yet is how a cold start gets a red card.
  if (status === null) return true;
  switch (status.kind) {
    case "loading":
    case "starting":
    case "parked":
    case "teamPending":
      return true;
    // `unreachable` is OUR network failing, not the container's. The roster's
    // own connection banner covers it; do not dress it as the box waking.
    case "ready":
    case "error":
    case "blocked":
    case "needsSignup":
    case "workspaceRequired":
    case "unreachable":
      return false;
  }
}
