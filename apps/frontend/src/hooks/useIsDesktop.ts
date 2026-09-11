"use client";

import { useSyncExternalStore } from "react";

const QUERY = "(min-width: 860px)";
// The chat shell's CSS mobile block is @media (max-width: 859px)
// (ChatLayout.css) — this is its exact complement, so the JS branch and the
// CSS branch can never disagree at any viewport width.

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
