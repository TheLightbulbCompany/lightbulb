"use client";

import * as React from "react";
import dynamic from "next/dynamic";
import { hasWebGL } from "@/scene/webgl";

const Scene = dynamic(() => import("@/scene/Scene"), { ssr: false, loading: () => null });

type Mode = "pending" | "scene" | "frozen" | "off";

/**
 * The 3D bulb — scroll-driven, or frozen at the strike under
 * prefers-reduced-motion. Without WebGL there is no scene: the page is the
 * HTML copy on the dark ground, which reads on its own.
 */
export function LightbulbScene() {
  const [mode, setMode] = React.useState<Mode>("pending");
  React.useEffect(() => {
    if (!hasWebGL()) {
      setMode("off");
      return;
    }
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setMode(media.matches ? "frozen" : "scene");
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  if (mode === "scene") return <Scene />;
  if (mode === "frozen") return <Scene frozen />;
  return null;
}
