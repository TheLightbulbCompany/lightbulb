"use client";

import { useCallback, useRef } from "react";

// Scrolls the message container directly — never via scrollIntoView.
// scrollIntoView walks every scrollable ancestor, and Chrome scrolls
// overflow-hidden boxes programmatically: the workspace shell (which keeps
// a viewport-sized dockview layer clipped below the fold) would shift the
// whole app upward with no way for the user to scroll back.
export function useScrollToBottom() {
  const containerRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = useCallback(() => {
    const el = containerRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, []);

  return {
    containerRef,
    scrollToBottom,
  };
}
