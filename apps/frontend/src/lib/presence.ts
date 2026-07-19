// Presence: is a human plausibly AT this tab right now?
//
// The browser's visibility signal alone cannot answer that — a display waking
// at 5am reports the frontmost tab "visible" with nobody in the room (the
// exact failure mode of the 2026-07-18 trial-burn incident). Presence is
// therefore visibility AND input recency.
//
// Signals: `keydown` (typing), `wheel` (desktop scroll — kept so desktop
// reading counts as present, avoiding a park under an active reader),
// `click` (a completed tap or mouse-click — a DELIBERATE action, and the
// only pointer signal that never fires on a scroll), and `pointerdown` ONLY
// for non-touch pointers (mouse/pen). A touch `pointerdown` is EXCLUDED
// because it fires on the first contact of a scroll-swipe — counting it
// would let a mobile user scrolling a parked chat to re-read wake the box,
// the exact "idle reading wakes it" leak this gate exists to close. A real
// touch TAP still registers via the `click` that follows it.
//
// Module-level singleton: listeners bind once per page, capture phase, and
// are passive — this must never affect scrolling performance.

const INPUT_EVENTS: Array<keyof WindowEventMap> = ["pointerdown", "keydown", "wheel", "click"];

let lastInputAt = typeof performance !== "undefined" ? performance.now() : 0;
let bound = false;

function bind() {
  if (bound || typeof window === "undefined") return;
  bound = true;
  for (const name of INPUT_EVENTS) {
    window.addEventListener(name, markInput, { capture: true, passive: true });
  }
}

function markInput(event: Event) {
  // Ignore touch pointerdowns — a scroll-swipe starts with one, and a real
  // tap is caught by the following `click`. Non-pointer events (keydown,
  // wheel, click) always count.
  if (event.type === "pointerdown" && (event as PointerEvent).pointerType === "touch") {
    return;
  }
  lastInputAt = performance.now();
}

/** Milliseconds since the last real input event. Initialized to the module
 * load time, so a fresh page load reads as recent input — deliberate: a
 * human navigating to the app IS presence for the mount-wake check. */
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
