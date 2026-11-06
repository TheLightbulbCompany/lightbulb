import { ThemeStamp } from "@/components/ThemeStamp";

export const dynamic = "force-dynamic";

export const metadata = {
  title: 'Lightbulb',
  description: 'AI agent chat',
}

export default function ChatLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <>
      {/* Stamps data-chat-theme before first paint (inline script) and on client navigation (effect). */}
      <ThemeStamp />
      <div data-private className="contents ph-no-capture">{children}</div>
    </>
  );
}
