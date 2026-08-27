import { SettingsGatewayGate } from "@/components/settings/SettingsGatewayGate";

export const metadata = {
  title: 'Settings - Lightbulb AI',
  description: 'Manage your agent settings and preferences',
}

export default function SettingsLayout({
  children,
}: {
  children: React.ReactNode
}) {
  // This file is a server component, so the client-side GatewayProvider wrap
  // lives in SettingsGatewayGate ("use client"). The provider gives the
  // gateway-bound config panels (Skills, Skill Proposals, Dreams, Channel
  // Status, MCP servers, the LLM health dot) the same WS the chat surface uses.
  return <SettingsGatewayGate>{children}</SettingsGatewayGate>;
}
