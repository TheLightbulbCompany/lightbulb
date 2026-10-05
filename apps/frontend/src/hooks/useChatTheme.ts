"use client";

import { useSyncExternalStore } from "react";

/**
 * The chat theme: light or dark. Chrome is always the neutral/white look —
 * there used to be a second "accent" axis (a cream/brand-hue option vs this
 * neutral one), removed 2026-09-25 so /chat has one consistent look and one
 * fewer setting to explain.
 *
 * State of record is the `data-chat-theme` attribute on <html>, written
 * before first paint by the inline script in app/chat/layout.tsx
 * (THEME_BOOTSTRAP_SCRIPT below is that script's source — keep the two in
 * step). Reading the DOM rather than a module variable is what lets the
 * pre-paint script and React agree on the very first render instead of
 * fighting over it.
 */

export type ChatTheme = "light" | "dark";

export const THEME_ATTR = "data-chat-theme";
export const THEME_KEY = "isol8:chat-theme";

export const DEFAULT_THEME: ChatTheme = "light";

/**
 * Runs inline and synchronously in <head> so the attribute exists before the
 * first paint. Without it every dark-mode load flashes white: /chat is
 * force-dynamic, so the server cannot know the preference and renders the
 * light default.
 *
 * When no theme is stored yet, this follows the OS preference
 * (`prefers-color-scheme`) instead of hardcoding light — a fresh device/
 * webview should open in the reader's own light/dark mode, not force light
 * until they find the toggle. A stored choice always wins over the OS.
 *
 * Wrapped in try/catch because localStorage throws outright in some privacy
 * modes — a theme preference is never worth blanking the page over.
 */
export const THEME_BOOTSTRAP_SCRIPT = `(function(){try{
var d=document.documentElement;
var t=localStorage.getItem(${JSON.stringify(THEME_KEY)});
if(t!=="dark"&&t!=="light"){
t=(window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches)?"dark":"light";
}
d.setAttribute(${JSON.stringify(THEME_ATTR)}, t);
}catch(e){
document.documentElement.setAttribute(${JSON.stringify(THEME_ATTR)},"light");
}})();`;

/** Same choice as THEME_BOOTSTRAP_SCRIPT (stored wins, else OS), for client code. */
export function resolveTheme(): ChatTheme {
  try {
    const t = localStorage.getItem(THEME_KEY);
    if (t === "dark" || t === "light") return t;
    return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  } catch {
    return DEFAULT_THEME;
  }
}

// One shared listener set: every hook consumer re-renders off the same write,
// so the rail toggle and the mobile footer toggle can never disagree.
const listeners = new Set<() => void>();

function subscribe(onStoreChange: () => void): () => void {
  listeners.add(onStoreChange);
  return () => {
    listeners.delete(onStoreChange);
  };
}

function emit(): void {
  listeners.forEach((l) => l());
}

function readTheme(): ChatTheme {
  return document.documentElement.getAttribute(THEME_ATTR) === "dark" ? "dark" : "light";
}

export function setChatTheme(theme: ChatTheme): void {
  document.documentElement.setAttribute(THEME_ATTR, theme);
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    // Private mode: the theme still applies for this session, it just won't
    // survive a reload. Not worth surfacing.
  }
  emit();
}

export interface ChatThemeState {
  theme: ChatTheme;
  toggleTheme: () => void;
}

export function useChatTheme(): ChatThemeState {
  // Server snapshot is the default, matching what the server actually
  // renders. The pre-paint script has already corrected the DOM by the time
  // hydration reads getSnapshot, so a dark user never sees a light frame.
  const theme = useSyncExternalStore(subscribe, readTheme, () => DEFAULT_THEME);

  return {
    theme,
    toggleTheme: () => setChatTheme(theme === "dark" ? "light" : "dark"),
  };
}
