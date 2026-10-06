"use client";

import { useEffect, useState, type ReactNode } from "react";
import { useDarkTheme } from "../MeetTile";

// Scales the fixed canvas: `fit` fills the window next to the thumbnail, otherwise a fixed `scale`.
export function PreviewScaler({ width, height, scale, fit, children }: { width: number; height: number; scale?: number; fit?: boolean; children: ReactNode }) {
  useDarkTheme();
  const [k, setK] = useState(scale ?? 0.5);
  useEffect(() => {
    if (!fit) return;
    const on = () => setK(Math.min((window.innerWidth - 32 - 24 - width / 4) / width, (window.innerHeight - 32) / height));
    on();
    window.addEventListener("resize", on);
    return () => window.removeEventListener("resize", on);
  }, [fit, width, height]);
  return (
    <div style={{ width: width * k, height: height * k, flexShrink: 0 }}>
      <div style={{ width, height, transform: `scale(${k})`, transformOrigin: "top left" }}>{children}</div>
    </div>
  );
}
