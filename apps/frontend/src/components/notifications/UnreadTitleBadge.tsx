"use client";

import { useEffect, useRef } from "react";
import { useNeedsYouCount } from "@/components/tasks/useRoomThreads";

/**
 * Owns the browser tab title: the place (`title`), prefixed with the count while
 * it's > 0: unread conversations plus your own waiting sessions (`ownWaitingCount`).
 * No UI — the badge is text in `document.title`. Without a `title` it keeps the page's own.
 */
export function UnreadTitleBadge({ unread, title }: { unread: number; title?: string }): null {
  const base = useRef("");

  useEffect(() => {
    base.current = document.title;
    return () => { document.title = base.current; };
  }, []);

  useEffect(() => {
    const where = title ?? base.current;
    document.title = unread > 0 ? `(${unread}) ${where}` : where;
    // The desktop app's dock icon carries the same count; an older build without
    // the permission just rejects, and the title still has it.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const dock = (window as any).__TAURI__?.window?.getCurrentWindow?.();
    dock?.setBadgeCount?.(unread > 0 ? unread : undefined)?.catch?.(() => {});
  }, [unread, title]);

  return null;
}

/** With the super-agent the tab title and Dock carry needsYouCount, the same number as the Activity badge and Home's
 *  "N need you" line (UX audit slice 2), not unread messages. */
export function NeedsYouTitleBadge({ title }: { title?: string }) {
  return <UnreadTitleBadge unread={useNeedsYouCount()} title={title} />;
}
