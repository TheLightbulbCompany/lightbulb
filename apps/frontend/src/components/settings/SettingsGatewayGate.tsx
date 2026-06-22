"use client";

import { GatewayProvider } from "@/hooks/useGateway";
import { useUnifiedNav } from "@/hooks/useUnifiedNav";

// Flag-gates the GatewayProvider wrap for /settings. `layout.tsx` is a server
// component and `useUnifiedNav` is a client hook, so the conditional wrap lives
// here.
//
// Flag-OFF (today's app): NO provider — /settings stays pure-REST, opening no
// idle WebSocket, byte-identical to the legacy surface.
//
// Flag-ON (unified nav): wrap in the same GatewayProvider the chat surface uses
// so the gateway-bound config panels (Skills, Skill Proposals, Dreams, Channel
// Status, MCP servers, the LLM health dot) can mount lazily.
export function SettingsGatewayGate({
  children,
}: {
  children: React.ReactNode;
}) {
  const unifiedNav = useUnifiedNav();
  return unifiedNav ? <GatewayProvider>{children}</GatewayProvider> : <>{children}</>;
}
