"use client";

import { useEffect, useState } from "react";
import type { IDockviewPanelHeaderProps } from "dockview-react";

// dockview's default tab closes through a bare <div>: no role, no name, no
// keyboard. Same markup/classes, with a real <button>.
export function DockTab({ api }: IDockviewPanelHeaderProps) {
  const [title, setTitle] = useState(api.title);
  useEffect(() => {
    setTitle(api.title);
    const d = api.onDidTitleChange((e) => setTitle(e.title));
    return () => d.dispose();
  }, [api]);
  return (
    <div className="dv-default-tab">
      <span className="dv-default-tab-content">{title}</span>
      <button
        type="button"
        className="dv-default-tab-action"
        aria-label={`Close ${title ?? "tab"}`}
        style={{ background: "none", border: 0, padding: 0, color: "inherit" }}
        onPointerDown={(e) => e.preventDefault()}
        onClick={(e) => {
          e.preventDefault();
          api.close();
        }}
      >
        <svg width="11" height="11" viewBox="0 0 28 28" aria-hidden="true" focusable="false">
          <path fill="currentColor" d="M2.1 27.3L0 25.2L11.9 13.3L0 1.4L2.1 0L14 11.9L25.9 0L28 1.4L16.1 13.3L28 25.2L25.9 27.3L14 15.4L2.1 27.3Z" />
        </svg>
      </button>
    </div>
  );
}
