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

  // Sync the latest matcher/handler/enabled into refs after each render —
  // this runs in an effect (not render body) so it doesn't trip the "no ref
  // writes during render" rule, and it settles before the next keydown can
  // read the refs below.
  React.useEffect(() => {
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
