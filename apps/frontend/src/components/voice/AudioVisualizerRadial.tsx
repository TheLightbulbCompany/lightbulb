/**
 * Copyright 2026 LiveKit, Inc.
 * SPDX-License-Identifier: Apache-2.0
 *
 * Ported from LiveKit Agents UI (livekit/components-js
 * packages/shadcn/components/agents-ui/agent-audio-visualizer-radial.tsx), Apache License 2.0.
 * Changed: no livekit-client / @livekit/components-react (bands come from our own analysers through `bands()`);
 * colours are our tokens via currentColor (globals.css .voice-viz-radial) instead of Tailwind classes; heights and
 * the highlight are written per frame through refs (useRadialAnimator); one size, tuned to the voice orb.
 */
"use client";

import { useRef } from "react";
import { useRadialAnimator, type RadialState } from "./useRadialAnimator";

export interface AudioVisualizerRadialProps {
  state: RadialState;
  /** Read once per animation frame: `barCount` values in 0..1. */
  bands: () => number[];
  /** Distance from the centre to the base of each bar, px. */
  radius?: number;
  /** Divisible by 4 for LiveKit's sequences to look right. */
  barCount?: number;
  className?: string;
}

/** Bars arranged in a circle that follow the agent's state and, while listening or speaking, the voice. */
export function AudioVisualizerRadial({ state, bands, radius = 24, barCount = 24, className }: AudioVisualizerRadialProps) {
  const root = useRef<HTMLDivElement>(null);
  const dotSize = (radius * Math.PI) / barCount;
  useRadialAnimator(root, state, barCount, dotSize, bands);

  return (
    <div ref={root} className={`voice-viz-radial ${className ?? ""}`} data-state={state} aria-hidden="true">
      {Array.from({ length: barCount }, (_, idx) => (
        <div key={idx} className="voice-viz-radial-slot" style={{ transform: `rotate(${(idx / barCount) * Math.PI * 2}rad) translateY(${radius}px)` }}>
          <div data-bar={idx} className="voice-viz-radial-bar" style={{ width: dotSize, minHeight: dotSize, height: dotSize }} />
        </div>
      ))}
    </div>
  );
}
