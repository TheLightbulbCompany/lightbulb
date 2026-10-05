import { THEME_BOOTSTRAP_SCRIPT } from "@/hooks/useChatTheme";

/**
 * Stamps data-chat-theme on <html> before first paint, so a product page
 * outside /chat gets the same neutral palette (and light/dark preference) as
 * /chat. Inline and synchronous on purpose — see app/chat/layout.tsx. Mount it
 * from the layout of each product route; marketing routes must not.
 */
export function ThemeStamp() {
  return <script dangerouslySetInnerHTML={{ __html: THEME_BOOTSTRAP_SCRIPT }} />;
}
