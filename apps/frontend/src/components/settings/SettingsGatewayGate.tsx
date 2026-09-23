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
 * The status read is cold (it starts nothing), so merely opening Settings costs one GET and
 * never wakes a runtime.
 */
function StatusPublisher() {
  // Not a bare `useOnboardingStatus` call: that hook needs the workspace-token plumbing this
  // one already owns — a forced-fresh mint whose `o.id` is verified against the active org
  // before a single fetch, which is the whole safety property of `useStatusLoaded`.
  useProvisioningState();
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
