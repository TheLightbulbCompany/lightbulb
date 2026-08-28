"use client";

/**
 * The hero lamp: a three.js pendant with a real light, or — on the server,
 * under prefers-reduced-motion, or without WebGL — the SVG lamp. Same store,
 * same contract, same triggers; only the rendering differs.
 *
 * The scene's canvas is not a control. The real control is the transparent
 * <button> positioned over the glass, so keyboard and screen-reader users
 * get "Turn on the light" like everyone else.
 */
import * as React from "react";
import dynamic from "next/dynamic";
import { HangingLamp } from "../HangingLamp";
import { hydrateFromStorage, strike, useLampTheme } from "./lampStore";
import { hasWebGL } from "./webgl";

const Scene = dynamic(() => import("./Scene"), { ssr: false, loading: () => null });

type Mode = "pending" | "scene" | "fallback";

export function HeroLamp() {
  const [mode, setMode] = React.useState<Mode>("pending");
  const theme = useLampTheme();

  React.useEffect(() => {
    hydrateFromStorage();
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    setMode(!reduce && hasWebGL() ? "scene" : "fallback");
  }, []);

  if (mode === "fallback") return <HangingLamp />;

  return (
    <>
      {mode === "scene" && <Scene />}
      <button
        type="button"
        className="ld-lamp-switch"
        aria-label={theme === "dark" ? "Turn on the light" : "Turn off the light"}
        onClick={strike}
      />
    </>
  );
}

export default HeroLamp;
