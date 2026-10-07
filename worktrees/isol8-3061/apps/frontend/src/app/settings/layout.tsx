import { SettingsGatewayGate } from "@/components/settings/SettingsGatewayGate";

export const metadata = {
  title: 'Settings - Lightbulb',
  description: 'Manage your agent settings and preferences',
}

export default function SettingsLayout({
  children,
}: {
  children: React.ReactNode
}) {
  // This file is a server component, so the client-side LiveEventsProvider
  // wrap lives in SettingsGatewayGate ("use client"). Runtime panels share its
  // canonical HTTP client and private AppSync invalidation channel.
  return <SettingsGatewayGate>{children}</SettingsGatewayGate>;
}
