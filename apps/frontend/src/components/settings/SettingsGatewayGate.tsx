"use client";

import { LiveEventsProvider } from "@/hooks/useLiveEvents";
import { useProvisioningState } from "@/hooks/useProvisioningState";

/**
 * Reads `/container/status` for its SIDE EFFECT: that hook is the one publisher of the
 * status-loaded signal (`useOnboardingStatus` → `setStatusScope`/`publishStatusLoaded`), and
 * `useStatusLoaded()` returns `false` for the whole life of a route that never mounts it.
 *
 * Before this, web had exactly two mount points — `ChatLayout` and `WorkspaceShell`, both
 * under `/chat`. A hard load of `/settings?panel=skills` therefore left every panel that
 * holds its RPC on a loaded status spinning forever, with no
 * error and no retry (review C1). A client-side nav in from `/chat` masked it, because this
 * module state survives the transition — which is exactly why it read as "works on my
 * machine".
 *
 * Fixed at the source rather than by special-casing the panels: any future panel that reads
 * the signal gets a real answer here, and the panels keep one rule ("hold until definite")
 * instead of one rule plus a per-route exception.
 *
 * **`wake: false` is load-bearing.** Before this publisher existed, `/settings` issued ZERO
 * `/container/status` requests — no caller of this hook is reachable from that route. The
 * default `wake: () => isPresent()` would therefore have made merely opening Settings (the
 * Billing tab included, which talks to no runtime at all) send `wake=1`, and `wake_policy`
 * suppresses only `wake=0`: a free-trial owner checking their plan would have started a
 * Fargate task or a microVM cold start, then polled at 5 s until it came up, because a parked
 * row is not `ready`. This reader wants to KNOW the runtime, not to be a reason to spend on
 * it. On a ready workspace `cadenceFor` returns 0, so it reads once and stops.
 */
function StatusPublisher() {
  // `wake: false` rather than a bare `useOnboardingStatus` call (the reviewer's suggestion):
  // that hook needs the workspace-token plumbing this one already owns — a forced-fresh mint
  // whose `o.id` is verified against the active org before a single fetch. Re-deriving it here
  // would duplicate ~60 lines whose failure mode is publishing another workspace's status, and
  // the scope is the whole safety property of `useStatusLoaded`. One option, no duplication.
  useProvisioningState({ wake: false });
  return null;
}

// Wraps /settings in the same live/runtime provider the chat surface uses so
// authenticated runtime panels can mount lazily. `layout.tsx` is a server
// component, so this client wrapper owns the provider.
export function SettingsGatewayGate({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <LiveEventsProvider>
      <StatusPublisher />
      {children}
    </LiveEventsProvider>
  );
}
