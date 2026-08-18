"use client";

import { useCallback, useRef } from "react";

// Scrolls the message container directly — never via scrollIntoView.
// scrollIntoView walks every scrollable ancestor, and Chrome scrolls
// overflow-hidden boxes programmatically: the workspace shell (which keeps
// a viewport-sized dockview layer clipped below the fold) would shift the
// whole app upward with no way for the user to scroll back.
export function useScrollToBottom() {
  const containerRef = useRef<HTMLDivElement>(null);
  // True for the brief window around a programmatic `scrollTop` write, so a
  // listener elsewhere (TranscriptSelectionBar's capture-phase scroll
  // dismissal) can tell that write apart from a real user scroll. See
  // markAutoScroll.
  const isAutoScrollingRef = useRef(false);

  // Call synchronously right before writing `scrollTop` anywhere (this
  // hook's own scrollToBottom, or a caller doing its own direct write, e.g.
  // MessageList's autoscroll/pagination-restore). The resulting native
  // `scroll` event fires asynchronously — the flag clears on the next tick,
  // after that event would already have been queued, so it can't swallow a
  // later real user scroll.
  const markAutoScroll = useCallback(() => {
    isAutoScrollingRef.current = true;
    setTimeout(() => {
      isAutoScrollingRef.current = false;
    }, 0);
  }, []);

  const scrollToBottom = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    markAutoScroll();
    el.scrollTop = el.scrollHeight;
  }, [markAutoScroll]);

  return {
    containerRef,
    scrollToBottom,
    isAutoScrollingRef,
    markAutoScroll,
  };
}
