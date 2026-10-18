"use client";

import type { RefObject } from "react";
import type { OutlineEntry } from "./documentOutline";

/** Scrolls the heading with the same text into view; no ids are injected into the rendered body. */
export function DocumentContents({ entries, bodyRef }: { entries: OutlineEntry[]; bodyRef: RefObject<HTMLElement | null> }) {
  const go = (entry: OutlineEntry) => {
    const headings = bodyRef.current?.querySelectorAll<HTMLElement>("h1, h2, h3") ?? [];
    const target = Array.from(headings).find((h) => (h.textContent ?? "").trim() === entry.text);
    target?.scrollIntoView({ block: "start", behavior: "smooth" });
  };
  return (
    <nav aria-label="Contents" className="mb-3 rounded-sm px-3 py-2 text-caption" style={{ background: "var(--plane-raised)" }}>
      <ul className="grid gap-1">
        {entries.map((e, i) => <li key={i} style={{ paddingLeft: `${(e.level - 1) * 12}px` }}>
          <button type="button" className="text-left hover:underline" style={{ color: "var(--fg-soft)" }} onClick={() => go(e)}>{e.text}</button>
        </li>)}
      </ul>
    </nav>
  );
}
