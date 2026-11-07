import { ThemeStamp } from "@/components/ThemeStamp";
import { VoiceShell } from "@/components/voice/VoiceShell";

export const dynamic = "force-dynamic";
export const metadata = { title: "Lightbulb", description: "Your assistant's calls and meetings" };

export default function VoiceLayout({ children }: { children: React.ReactNode }) {
  return <>
    <ThemeStamp />
    <div data-private className="contents ph-no-capture"><VoiceShell>{children}</VoiceShell></div>
  </>;
}
