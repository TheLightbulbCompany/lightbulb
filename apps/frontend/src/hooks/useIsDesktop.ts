"use client";

import { useSyncExternalStore } from "react";

const QUERY = "(min-width: 600px)";
// The chat shell's CSS mobile block is @media (max-width: 599px)
// (ChatLayout.css) — this is its exact complement, so the JS branch and the
// CSS branch can never disagree at any viewport width.
// 600, not 860: a narrow laptop window (and the desktop app, whose minimum
// width is 800) must keep the workspace shell; only phone widths get the
// single-pane one. Between 600 and 859 the room panel still uses its own
// narrow rules (messaging.css, RoomPanel's narrowQuery), which stay at 859.

function subscribe(onStoreChange: () => void): () => void {
  const mql = window.matchMedia(QUERY);
  mql.addEventListener("change", onStoreChange);
  return () => mql.removeEventListener("change", onStoreChange);
}

function getSnapshot(): boolean {
  return window.matchMedia(QUERY).matches;
}

export function useIsDesktop(): boolean {
  // Server snapshot false: SSR renders the mobile shell; desktop swaps in on
  // hydration. Chat state survives the swap via module stores.
  return useSyncExternalStore(subscribe, getSnapshot, () => false);
}
