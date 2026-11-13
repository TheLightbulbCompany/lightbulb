import { ThemeStamp } from "@/components/ThemeStamp";

export const metadata = {
  title: 'Settings - Lightbulb',
  description: 'Manage your agent settings and preferences',
}

// /settings only forwards to /voice/settings now (OAuth, Stripe and billing returns still land here).
export default function SettingsLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <>
      <ThemeStamp />
      {children}
    </>
  );
}
