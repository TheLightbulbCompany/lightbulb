"use client";

import { LiveEventsProvider } from "@/hooks/useLiveEvents";
import { useProvisioningState } from "@/hooks/useProvisioningState";

/**
 * Reads `/container/status` for its SIDE EFFECT: that hook is the one publisher of the
 * runtime host (`useOnboardingStatus` → `setRuntimeHostScope`/`publishRuntimeHost`), and
 * `useRuntimeHost()` returns `undefined` for the whole life of a route that never mounts it.
 *
 * Before this, web had exactly two mount points — `ChatLayout` and `WorkspaceShell`, both
 * under `/chat`. A hard load of `/settings?panel=skills` therefore left every panel that
 * holds its RPC on a definite host spinning forever, on ECS as much as AgentCore, with no
 * error and no retry (review C1). A client-side nav in from `/chat` masked it, because this
 * module state survives the transition — which is exactly why it read as "works on my
 * machine".
 *
 * Fixed at the source rather than by special-casing the panels: any future panel that reads
 * the host gets a real answer here, and the panels keep one rule ("hold until definite")
 * instead of one rule plus a per-route exception.
 *
 * Cost: one `/container/status` read. `cadenceFor` returns 0 once the container is ready, so
 * a settled workspace polls once and stops; an unready one polls at the same 5 s/15 s
 * cadence `/chat` uses — which the Overview panel already started on this route whenever it
 * was the selected tab, so this generalizes existing behaviour rather than adding a kind.
 */
function RuntimeHostPublisher() {
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
      <RuntimeHostPublisher />
      {children}
    </LiveEventsProvider>
  );
}
