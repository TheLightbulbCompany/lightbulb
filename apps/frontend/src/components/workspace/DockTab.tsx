"use client";

import { useCallback, useEffect, useRef, useSyncExternalStore } from "react";
import type { IDockviewPanelHeaderProps } from "dockview-react";

// dockview's default tab closes through a bare <div> with no keyboard path.
// dockview wraps this renderer in its own role="tab", so the close control
// stays non-interactive for AT (no nested button); keyboard users close the
// focused tab with Delete, announced via aria-keyshortcuts.
export function DockTab({ api }: IDockviewPanelHeaderProps) {
  const title = useSyncExternalStore(
    useCallback((cb) => {
      const d = api.onDidTitleChange(cb);
      return () => d.dispose();
    }, [api]),
    () => api.title,
    () => api.title,
  );
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const tab = root.current?.closest<HTMLElement>('[role="tab"]');
    if (!tab) return;
    tab.setAttribute("aria-keyshortcuts", "Delete");
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Delete" && e.target === tab) api.close();
    };
    tab.addEventListener("keydown", onKey);
    return () => tab.removeEventListener("keydown", onKey);
  }, [api]);
  return (
    <div className="dv-default-tab" ref={root}>
      <span className="dv-default-tab-content">{title}</span>
      <span
        className="dv-default-tab-action"
        aria-hidden="true"
        onPointerDown={(e) => e.preventDefault()}
        onClick={(e) => {
          e.preventDefault();
          api.close();
        }}
      >
        <svg width="11" height="11" viewBox="0 0 28 28" focusable="false">
          <path fill="currentColor" d="M2.1 27.3L0 25.2L11.9 13.3L0 1.4L2.1 0L14 11.9L25.9 0L28 1.4L16.1 13.3L28 25.2L25.9 27.3L14 15.4L2.1 27.3Z" />
        </svg>
      </span>
    </div>
  );
}
