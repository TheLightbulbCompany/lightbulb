"use client";

/**
 * The hero lamp: a three.js pendant with a real light, or — on the server,
 * under prefers-reduced-motion, or without WebGL — the SVG lamp. Same store,
 * same contract, same triggers; only the rendering differs.
 *
 * The scene's canvas is not a control. The real control is the transparent
 * <button> positioned over the glass, so keyboard and screen-reader users
 * get "Turn on the light" like everyone else. The lamp now moves with the
 * cursor, so the button moves with it: the scene projects the glass to canvas
 * pixels every frame and writes the button's transform.
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
  const switchRef = React.useRef<HTMLButtonElement>(null);

  React.useEffect(() => {
    hydrateFromStorage();
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    setMode(!reduce && hasWebGL() ? "scene" : "fallback");
  }, []);

  if (mode === "fallback") return <HangingLamp />;

  return (
    <>
      {/* `pending` deliberately draws no lamp: swapping an SVG lamp for a 3D
          one a frame later is a worse first impression than an unlit room the
          lamp arrives into. Scripts off is the one case that never resolves,
          so <noscript> gets the SVG — its effects cannot run there anyway. */}
      {mode === "scene" && <Scene switchRef={switchRef} />}
      <noscript>
        <HangingLamp />
      </noscript>
      <button
        ref={switchRef}
        type="button"
        className="ld-lamp-switch"
        aria-label={theme === "dark" ? "Turn on the light" : "Turn off the light"}
        onClick={strike}
      />
    </>
  );
}

export default HeroLamp;
