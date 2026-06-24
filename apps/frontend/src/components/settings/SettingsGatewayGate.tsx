"use client";

import { GatewayProvider } from "@/hooks/useGateway";

// Wraps /settings in the same GatewayProvider the chat surface uses so the
// gateway-bound config panels (Skills, Skill Proposals, Dreams, Channel Status,
// MCP servers, the LLM health dot) can mount lazily. `layout.tsx` is a server
// component, so this client wrapper owns the provider.
export function SettingsGatewayGate({
  children,
}: {
  children: React.ReactNode;
}) {
  return <GatewayProvider>{children}</GatewayProvider>;
}
