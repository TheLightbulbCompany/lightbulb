"use client";

import { useEffect, useRef } from "react";

/**
 * Prefixes the browser tab title with the unread count while it's > 0. No
 * UI — the badge is text in `document.title`.
 */
export function UnreadTitleBadge({ unread }: { unread: number }): null {
  const base = useRef("");

  useEffect(() => {
    base.current = document.title;
    return () => { document.title = base.current; };
  }, []);

  useEffect(() => {
    document.title = unread > 0 ? `(${unread}) ${base.current}` : base.current;
  }, [unread]);

  return null;
}
