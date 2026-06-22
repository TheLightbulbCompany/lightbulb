import { SettingsGatewayGate } from "@/components/settings/SettingsGatewayGate";

export const metadata = {
  title: 'Settings - isol8',
  description: 'Manage your agent settings and preferences',
}

export default function SettingsLayout({
  children,
}: {
  children: React.ReactNode
}) {
  // The GatewayProvider wrap is flag-gated on the unified nav. This file is a
  // server component and the flag is a client hook, so the conditional wrap
  // lives in SettingsGatewayGate ("use client"):
  //   flag-OFF (today's app): NO provider — /settings stays pure-REST, opens no
  //     idle WebSocket, byte-identical to the legacy surface.
  //   flag-ON (unified nav): wraps in the same GatewayProvider the chat surface
  //     uses so the gateway-bound config panels (Skills, Skill Proposals,
  //     Dreams, Channel Status, MCP servers, the LLM health dot) can mount
  //     byte-identical to their Control-router form.
  return <SettingsGatewayGate>{children}</SettingsGatewayGate>;
}
