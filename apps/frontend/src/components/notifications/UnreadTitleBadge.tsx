"use client";

import { useEffect, useRef } from "react";

/**
 * Prefixes the browser tab title with the count while it's > 0: unread
 * conversations plus your own sessions needing you (`sessionsNeedingYou`).
 * No UI — the badge is text in `document.title`.
 */
export function UnreadTitleBadge({ unread }: { unread: number }): null {
  const base = useRef("");

  useEffect(() => {
    base.current = document.title;
    return () => { document.title = base.current; };
  }, []);

  useEffect(() => {
    document.title = unread > 0 ? `(${unread}) ${base.current}` : base.current;
    // The desktop app's dock icon carries the same count; an older build without
    // the permission just rejects, and the title still has it.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const dock = (window as any).__TAURI__?.window?.getCurrentWindow?.();
    dock?.setBadgeCount?.(unread > 0 ? unread : undefined)?.catch?.(() => {});
  }, [unread]);

  return null;
}
