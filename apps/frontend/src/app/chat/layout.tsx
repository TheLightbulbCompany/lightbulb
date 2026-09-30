import { THEME_BOOTSTRAP_SCRIPT } from "@/hooks/useChatTheme";

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
  return (
    <>
      {/*
        Stamps data-chat-theme on <html> BEFORE first paint.
        This route is force-dynamic and the server has no way to know the
        preference, so it renders the light default — without this script a
        dark-mode user gets a full white frame on every single load.

        It has to be inline and synchronous for that reason: a <Script> with
        any strategy, or anything deferred, runs after the first paint and the
        flash is back. dangerouslySetInnerHTML is the only way to emit a bare
        inline script here; the content is a module constant, never user input.
      */}
      <script dangerouslySetInnerHTML={{ __html: THEME_BOOTSTRAP_SCRIPT }} />
      <div data-private className="contents ph-no-capture">{children}</div>
    </>
  );
}
