/**
 * Web's `VisibilityAdapter` for `useOnboardingStatus` (`@lightbulb/shared`).
 *
 * Deliberately `document.visibilityState`, NOT presence (`@/lib/presence`'s
 * `isPresent`, which additionally requires recent input): the transition back
 * to visible is what refetches the status after a tab spent time hidden,
 * covering a live event AppSync dropped meanwhile.
 */
"use client";

import { useSyncExternalStore } from "react";
import type { VisibilityAdapter } from "@lightbulb/shared";

export const webVisibilityAdapter: VisibilityAdapter = {
  isVisible: () =>
    typeof document === "undefined" || document.visibilityState !== "hidden",
  subscribe: (onChange) => {
    if (typeof document === "undefined") return () => {};
    document.addEventListener("visibilitychange", onChange);
    return () => document.removeEventListener("visibilitychange", onChange);
  },
};

/** The page is not hidden (backgrounded tab, minimized window). True on the server. */
export function usePageVisible(): boolean {
  return useSyncExternalStore(webVisibilityAdapter.subscribe, webVisibilityAdapter.isVisible, () => true);
}
