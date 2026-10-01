"use client";

import { useEffect, useState, type ReactNode } from "react";

const CHARS_PER_TICK = 40;
const TICK_MS = 100;
const key = (playKey: string) => `lightbulb.reveal.${playKey}`;

function played(playKey: string): boolean {
  try { return window.sessionStorage.getItem(key(playKey)) === "1"; } catch { return true; }
}
function markPlayed(playKey: string): void {
  try { window.sessionStorage.setItem(key(playKey), "1"); } catch { /* storage blocked: play once per mount */ }
}
function reducedMotion(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function"
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** Reveals `text` once per `playKey`, 40 characters per 100 ms; whole text at once under
 *  reduced motion or after it has played. Render-prop so the caller keeps its own markup. */
export function TypingReveal({ text, playKey, children }: {
  text: string; playKey: string; children: (shown: string) => ReactNode;
}) {
  const [count, setCount] = useState(() => (played(playKey) || reducedMotion() ? text.length : 0));
  const done = count >= text.length;
  useEffect(() => {
    if (done) { markPlayed(playKey); return; }
    const timer = setInterval(() => setCount((c) => Math.min(text.length, c + CHARS_PER_TICK)), TICK_MS);
    return () => clearInterval(timer);
  }, [done, text.length, playKey]);
  return <>{children(text.slice(0, count))}</>;
}
