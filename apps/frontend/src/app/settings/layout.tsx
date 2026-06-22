import { GatewayProvider } from "@/hooks/useGateway";

export const metadata = {
  title: 'Settings - isol8',
  description: 'Manage your agent settings and preferences',
}

export default function SettingsLayout({
  children,
}: {
  children: React.ReactNode
}) {
  // Wrap every /settings/* route in the same GatewayProvider the chat surface
  // uses so gateway-bound panels (Skills, Skill Proposals, Dreams, Channel
  // Status, MCP servers, the LLM health dot) can mount byte-identical to their
  // Control-router form — no per-panel provider plumbing. Opens a per-container
  // WS lazily in Settings, mirroring the /teams layout precedent.
  return <GatewayProvider>{children}</GatewayProvider>;
}
