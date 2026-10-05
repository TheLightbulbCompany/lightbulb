"use client";

import { useLayoutEffect } from "react";

import { THEME_ATTR, THEME_BOOTSTRAP_SCRIPT, resolveTheme } from "@/hooks/useChatTheme";

/**
 * Owns data-chat-theme on <html> for a product route (/chat and every page
 * that should look like it). The inline script stamps before first paint on a
 * full load; React does not execute it on a client navigation, so a layout effect (before paint)
 * stamps then too. On unmount it removes the attribute so a marketing page
 * reached next is not left neutral. Two stamped routes in a row cannot flash:
 * the old tree's cleanup runs before the new tree's effect in one commit.
 */
export function ThemeStamp() {
  useLayoutEffect(() => {
    const root = document.documentElement;
    if (!root.hasAttribute(THEME_ATTR)) root.setAttribute(THEME_ATTR, resolveTheme());
    return () => root.removeAttribute(THEME_ATTR);
  }, []);
  return <script dangerouslySetInnerHTML={{ __html: THEME_BOOTSTRAP_SCRIPT }} />;
}
