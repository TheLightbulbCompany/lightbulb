"use client";

import { useSyncExternalStore } from "react";

/**
 * The two independent axes of the chat theme.
 *
 * `theme` moves the plane ladder (rail / sidebar / pane) between light and
 * dark. `accent` decides how much brand hue the chrome carries. They are
 * deliberately NOT one setting: focus is legible in both themes, and the
 * semantic colours (unread, mention, presence, run state) are unaffected by
 * either.
 *
 * State of record is the pair of attributes on <html>, written before first
 * paint by the inline script in app/chat/layout.tsx (THEME_BOOTSTRAP_SCRIPT
 * below is that script's source — keep the two in step). Reading the DOM
 * rather than a module variable is what lets the pre-paint script and React
 * agree on the very first render instead of fighting over it.
 */

export type ChatTheme = "light" | "dark";
export type ChatAccent = "full" | "focus";

export const THEME_ATTR = "data-chat-theme";
export const ACCENT_ATTR = "data-chat-accent";
export const THEME_KEY = "isol8:chat-theme";
export const ACCENT_KEY = "isol8:chat-accent";

export const DEFAULT_THEME: ChatTheme = "light";
export const DEFAULT_ACCENT: ChatAccent = "full";

/**
 * Runs inline and synchronously in <head> so the attributes exist before the
 * first paint. Without it every dark-mode load flashes white: /chat is
 * force-dynamic, so the server cannot know the preference and renders the
 * light default.
 *
 * Wrapped in try/catch because localStorage throws outright in some privacy
 * modes — a theme preference is never worth blanking the page over.
 */
export const THEME_BOOTSTRAP_SCRIPT = `(function(){try{
var d=document.documentElement;
var t=localStorage.getItem(${JSON.stringify(THEME_KEY)});
var a=localStorage.getItem(${JSON.stringify(ACCENT_KEY)});
d.setAttribute(${JSON.stringify(THEME_ATTR)}, t==="dark"?"dark":"light");
d.setAttribute(${JSON.stringify(ACCENT_ATTR)}, a==="focus"?"focus":"full");
}catch(e){
document.documentElement.setAttribute(${JSON.stringify(THEME_ATTR)},"light");
document.documentElement.setAttribute(${JSON.stringify(ACCENT_ATTR)},"full");
}})();`;

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

function readAccent(): ChatAccent {
  return document.documentElement.getAttribute(ACCENT_ATTR) === "focus" ? "focus" : "full";
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

export function setChatAccent(accent: ChatAccent): void {
  document.documentElement.setAttribute(ACCENT_ATTR, accent);
  try {
    localStorage.setItem(ACCENT_KEY, accent);
  } catch {
    // See setChatTheme.
  }
  emit();
}

export interface ChatThemeState {
  theme: ChatTheme;
  accent: ChatAccent;
  toggleTheme: () => void;
  toggleAccent: () => void;
}

export function useChatTheme(): ChatThemeState {
  // Server snapshots are the defaults, matching what the server actually
  // renders. The pre-paint script has already corrected the DOM by the time
  // hydration reads getSnapshot, so a dark user never sees a light frame.
  const theme = useSyncExternalStore(subscribe, readTheme, () => DEFAULT_THEME);
  const accent = useSyncExternalStore(subscribe, readAccent, () => DEFAULT_ACCENT);

  return {
    theme,
    accent,
    toggleTheme: () => setChatTheme(theme === "dark" ? "light" : "dark"),
    toggleAccent: () => setChatAccent(accent === "focus" ? "full" : "focus"),
  };
}
