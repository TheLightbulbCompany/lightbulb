export const dynamic = "force-dynamic";

export const metadata = {
  title: 'Chat - Lightbulb',
  description: 'AI agent chat',
}

export default function ChatLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return <>{children}</>;
}
