import { ThemeStamp } from "@/components/ThemeStamp";
import { LiveEventsProvider } from "@/hooks/useLiveEvents";
import { VoiceShell } from "@/components/voice/VoiceShell";

export const dynamic = "force-dynamic";
export const metadata = { title: "Lightbulb", description: "Your assistant's calls and meetings" };

export default function VoiceLayout({ children }: { children: React.ReactNode }) {
  return <>
    <ThemeStamp />
    {/* The Settings panels (Connections → GoogleAccountsSection → useAgents) read live events; without this
        provider the first connected Google account threw and blanked /voice/settings (7 Nov). */}
    <div data-private className="contents ph-no-capture"><LiveEventsProvider><VoiceShell>{children}</VoiceShell></LiveEventsProvider></div>
  </>;
}
