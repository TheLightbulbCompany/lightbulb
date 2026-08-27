/**
 * Web's `VisibilityAdapter` for `useOnboardingStatus` (`@lightbulb/shared`).
 *
 * Deliberately `document.visibilityState`, NOT presence (`@/lib/presence`'s
 * `isPresent`, which additionally requires recent input): occlusion — a tab
 * covered by another window but not minimized/switched-away — never flips
 * `visibilityState`, which is exactly what lets the bounded loop's backstop
 * keep ticking through Chrome's occlusion throttling (the scenario
 * `useOnboardingStatus`'s module doc and the CLIENT-HOOK research pin as the
 * real freeze). A genuinely hidden/backgrounded tab is what pauses it.
 */
"use client";

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
