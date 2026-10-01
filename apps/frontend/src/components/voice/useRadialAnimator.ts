/**
 * Copyright 2026 LiveKit, Inc.
 * SPDX-License-Identifier: Apache-2.0
 *
 * Ported from LiveKit Agents UI (livekit/components-js
 * packages/shadcn/hooks/agents-ui/use-agent-audio-visualizer-radial.ts), Apache License 2.0.
 * Changed: no @livekit/components-react; one requestAnimationFrame loop writes the highlight and the
 * bar heights straight to the DOM through refs, so React never re-renders per step or per frame.
 */
import { useEffect, useRef, type RefObject } from "react";

export type RadialState = "connecting" | "listening" | "thinking" | "speaking";

function findGcdLessThan(columns: number, max: number = columns): number {
  function gcd(a: number, b: number): number {
    while (b !== 0) {
      const t = b;
      b = a % b;
      a = t;
    }
    return a;
  }
  for (let i = max; i >= 1; i--) {
    if (gcd(columns, i) === i) return i;
  }
  return 1;
}

function generateConnectingSequenceBar(columns: number): number[][] {
  const center = Math.floor(columns / 2);
  return Array.from({ length: columns }, (_, x) => [x, (x + center) % columns]);
}

function generateListeningSequenceBar(columns: number): number[][] {
  const divisor = columns > 8 ? columns / findGcdLessThan(columns, 4) : findGcdLessThan(columns, 2);
  return Array.from({ length: divisor }, (_, idx) =>
    Array.from({ length: Math.floor(columns / divisor) }, (_, idx2) => idx2 * divisor + idx),
  );
}

/** The highlight steps for a state: each entry is the set of lit bars, advanced every `interval` ms. */
export function radialSequence(state: RadialState, barCount: number): { steps: number[][]; interval: number } {
  switch (state) {
    case "connecting":
      return { steps: generateConnectingSequenceBar(barCount), interval: 500 };
    case "listening":
      return { steps: generateListeningSequenceBar(barCount), interval: 500 };
    case "thinking":
      return { steps: generateListeningSequenceBar(barCount), interval: Infinity }; // still; the ring spins (CSS)
    case "speaking":
      return { steps: [Array.from({ length: barCount }, (_, i) => i)], interval: 1000 };
  }
}

// ponytail: LiveKit grows a bar to dotSize * 10 * band; 5 keeps the loudest bar inside the room-call avatar ring.
const GROWTH = 5;

/**
 * Drives the bars under `root` (each `[data-bar]`, in index order): lights the current sequence step and, while
 * listening or speaking, sizes each bar from `bands()` (0..1, one value per bar).
 */
export function useRadialAnimator(root: RefObject<HTMLElement | null>, state: RadialState, barCount: number, dotSize: number, bands: () => number[]) {
  const latest = useRef(bands); // a new bands() (the orb swaps mic for output) must not restart the sequence
  useEffect(() => { latest.current = bands; });
  useEffect(() => {
    const bars = Array.from(root.current?.querySelectorAll<HTMLElement>("[data-bar]") ?? []);
    const { steps, interval } = radialSequence(state, barCount);
    const sized = state === "listening" || state === "speaking";
    let lit = -1;
    let id = 0;
    const start = performance.now();
    const frame = (now: number) => {
      const step = Number.isFinite(interval) ? Math.floor(Math.max(0, now - start) / interval) % steps.length : 0;
      if (step !== lit) {
        lit = step;
        const on = new Set(steps[step]);
        bars.forEach((bar, i) => { bar.dataset.highlighted = String(on.has(i)); });
      }
      const levels = sized ? latest.current() : null;
      bars.forEach((bar, i) => { bar.style.height = `${Math.max(dotSize, dotSize * GROWTH * (levels?.[i] ?? 0))}px`; });
      id = requestAnimationFrame(frame);
    };
    frame(start);
    return () => cancelAnimationFrame(id);
  }, [root, state, barCount, dotSize]);
}
