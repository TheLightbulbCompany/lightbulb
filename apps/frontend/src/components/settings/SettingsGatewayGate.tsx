"use client";

import { LiveEventsProvider } from "@/hooks/useLiveEvents";

// Wraps /settings in the same live/runtime provider the chat surface uses so
// authenticated runtime panels can mount lazily. `layout.tsx` is a server
// component, so this client wrapper owns the provider.
export function SettingsGatewayGate({
  children,
}: {
  children: React.ReactNode;
}) {
  return <LiveEventsProvider>{children}</LiveEventsProvider>;
}
