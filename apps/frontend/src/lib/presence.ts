// Presence: is a human plausibly AT this tab right now?
//
// The browser's visibility signal alone cannot answer that — a display waking
// at 5am reports the frontmost tab "visible" with nobody in the room (the
// exact failure mode of the 2026-07-18 trial-burn incident). Presence is
// therefore visibility AND input recency: pointerdown/keydown/wheel within
// the window. Deliberately NOT touchstart — on mobile every scroll-swipe
// fires it, which would leak wakes from idle reading (verified in the
// wake-design review); wheel is included so desktop reading (scroll without
// clicks) still counts as present for PARK purposes.
//
// Module-level singleton: listeners bind once per page, capture phase, and
// are passive — this must never affect scrolling performance.

const INPUT_EVENTS: Array<keyof WindowEventMap> = ["pointerdown", "keydown", "wheel"];

let lastInputAt = typeof performance !== "undefined" ? performance.now() : 0;
let bound = false;

function bind() {
  if (bound || typeof window === "undefined") return;
  bound = true;
  for (const name of INPUT_EVENTS) {
    window.addEventListener(name, markInput, { capture: true, passive: true });
  }
}

function markInput() {
  lastInputAt = performance.now();
}

/** Milliseconds since the last real input event (Infinity before any). */
export function lastInputAgeMs(): number {
  bind();
  return performance.now() - lastInputAt;
}

/**
 * Visible AND touched within `recencyMs`. The default 120s matches the
 * wake-request recency gate; park scheduling passes its own longer windows.
 */
export function isPresent(recencyMs = 120_000): boolean {
  bind();
  if (typeof document === "undefined") return false;
  return document.visibilityState === "visible" && lastInputAgeMs() < recencyMs;
}

// Bind eagerly on module load in the browser so the recency clock starts at
// page load, not at first query.
bind();
