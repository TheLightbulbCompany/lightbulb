/**
 * The lamp's state, shared by the three.js scene, the SVG fallback and the
 * hero's input. A module-level store instead of context because the writer
 * (HeroFunnel) and the reader (the lamp) are siblings in page.tsx, and a
 * provider around the whole page would be scaffolding for one boolean.
 *
 * Contract (unchanged from the SVG lamp): `data-lb-theme` on <html>,
 * persisted under `lightbulb-theme`. Only `.ld-page` reads the attribute, so
 * this cannot repaint /chat. Server snapshot is always "dark": the room
 * starts unlit, and hydration must not depend on storage.
 */
import { useSyncExternalStore } from "react";

export type LampTheme = "dark" | "light";
export const LAMP_STORAGE_KEY = "lightbulb-theme";

let theme: LampTheme = "dark";
const listeners = new Set<() => void>();
let themingTimer: ReturnType<typeof setTimeout> | undefined;

/**
 * Marks <html> for the strike's duration so every landing surface eases
 * with the hero's wall (`landing.css` reads `data-lb-theming`; the theme
 * flips custom properties, and descendants consuming them would otherwise
 * snap). A second strike inside the window extends it rather than cutting
 * the first transition short.
 */
function markTheming() {
  if (typeof document === "undefined") return;
  document.documentElement.setAttribute("data-lb-theming", "");
  clearTimeout(themingTimer);
  themingTimer = setTimeout(() => document.documentElement.removeAttribute("data-lb-theming"), 800);
}

function write(t: LampTheme) {
  if (typeof document !== "undefined") {
    document.documentElement.setAttribute("data-lb-theme", t);
  }
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(LAMP_STORAGE_KEY, t);
  } catch {
    // private mode — the toggle still works for this page view
  }
}

export function getTheme(): LampTheme {
  return theme;
}

export function getServerTheme(): LampTheme {
  return "dark";
}

export function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

function commit(t: LampTheme): boolean {
  const changed = t !== theme;
  theme = t;
  write(t); // keep the DOM/storage in step even on a no-op set
  if (changed) listeners.forEach((cb) => cb());
  return changed;
}

export function setTheme(t: LampTheme): void {
  if (commit(t)) markTheming();
}

export function strike(): void {
  setTheme(theme === "dark" ? "light" : "dark");
}

export function lightOn(): void {
  if (theme !== "light") setTheme("light");
}

export function hydrateFromStorage(): void {
  let v: string | null = null;
  try {
    v = window.localStorage.getItem(LAMP_STORAGE_KEY);
  } catch {
    // private mode — fall through to the default
  }
  // A returning visitor's stored theme is adopted without the strike
  // transition, for the same reason the scene seeds its lit state instead
  // of replaying the strike: the page arrives in that state, it doesn't
  // move into it.
  if (v === "light" || v === "dark") commit(v);
  else write(theme);
}

export function useLampTheme(): LampTheme {
  return useSyncExternalStore(subscribe, getTheme, getServerTheme);
}

/**
 * The visitor's idea, mirrored onto the laptop's screen in the scene.
 * Same store as the theme because the same keystroke writes both — and the
 * scene subscribes once for either.
 */
let ideaText = "";

export function getIdeaText(): string {
  return ideaText;
}

export function setIdeaText(t: string): void {
  if (t === ideaText) return;
  ideaText = t;
  listeners.forEach((cb) => cb());
}

export function useIdeaText(): string {
  return useSyncExternalStore(subscribe, getIdeaText, () => "");
}
