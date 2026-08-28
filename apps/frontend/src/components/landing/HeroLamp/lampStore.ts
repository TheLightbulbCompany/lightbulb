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

function write(t: LampTheme) {
  if (typeof document !== "undefined") {
    document.documentElement.setAttribute("data-lb-theme", t);
  }
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

export function setTheme(t: LampTheme): void {
  if (t === theme) {
    write(t); // keep the DOM/storage in step even on a no-op set
    return;
  }
  theme = t;
  write(t);
  listeners.forEach((cb) => cb());
}

export function strike(): void {
  setTheme(theme === "dark" ? "light" : "dark");
}

export function lightOn(): void {
  if (theme !== "light") setTheme("light");
}

export function hydrateFromStorage(): void {
  try {
    const v = window.localStorage.getItem(LAMP_STORAGE_KEY);
    if (v === "light" || v === "dark") setTheme(v);
    else write(theme);
  } catch {
    write(theme);
  }
}

export function useLampTheme(): LampTheme {
  return useSyncExternalStore(subscribe, getTheme, getServerTheme);
}
