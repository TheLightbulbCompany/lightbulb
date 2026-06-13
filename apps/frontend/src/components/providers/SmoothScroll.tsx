"use client";

import { ReactLenis } from "lenis/react";

export function SmoothScroll({ children }: { children: React.ReactNode }) {
  // Light smoothing only — heavy lerp/duration values make the page feel
  // like it's fighting the scroll wheel.
  return (
    <ReactLenis root options={{ lerp: 0.18, duration: 0.9 }}>
      {children}
    </ReactLenis>
  );
}
