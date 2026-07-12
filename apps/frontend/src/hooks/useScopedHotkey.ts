"use client";

import * as React from "react";

/**
 * Window-level keydown listener scoped to a focus owner. With several
 * panels of the same kind docked side by side, a global hotkey must not
 * fire in all of them — only the instance whose subtree contains
 * `document.activeElement` responds.
 *
 * One listener per instance; matcher/handler are read via refs so callers
 * don't need to memoize them across renders.
 */
export function useScopedHotkey(
  ref: React.RefObject<HTMLElement | null>,
  matcher: (e: KeyboardEvent) => boolean,
  handler: () => void,
  enabled = true,
): void {
  const matcherRef = React.useRef(matcher);
  const handlerRef = React.useRef(handler);
  const enabledRef = React.useRef(enabled);

  // Sync the latest matcher/handler/enabled into refs on every commit. A
  // LAYOUT effect (not a passive one) is load-bearing here: the keydown
  // listener is a raw window listener with no React event-delegation flush
  // protection, so a native keystroke landing between paint and a passive
  // effect flush would read the previous render's values. useLayoutEffect
  // runs synchronously with commit, closing that stale-closure window.
  React.useLayoutEffect(() => {
    matcherRef.current = matcher;
    handlerRef.current = handler;
    enabledRef.current = enabled;
  });

  React.useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (!matcherRef.current(e)) return;
      if (!enabledRef.current) return;
      if (!ref.current?.contains(document.activeElement)) return;
      e.preventDefault();
      handlerRef.current();
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [ref]);
}
