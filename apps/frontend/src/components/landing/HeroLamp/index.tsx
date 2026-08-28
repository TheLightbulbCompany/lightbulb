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
import { clearTheming, hydrateFromStorage, strike, useLampTheme } from "./lampStore";
import { hasWebGL } from "./webgl";

const Scene = dynamic(() => import("./Scene"), { ssr: false, loading: () => null });

// Every nav on the landing chrome is a soft nav, and a passive effect's cleanup
// is flushed AFTER paint — so "/" → "/blog" painted a frame of the dark room and
// "/blog" → "/" a frame of cream. The mutation phase is before paint, so both the
// write and the removal land with the DOM swap. useLayoutEffect warns during SSR,
// where it would never run anyway; useEffect on the server is the same no-op.
const useLayout = typeof window === "undefined" ? React.useEffect : React.useLayoutEffect;

type Mode = "pending" | "scene" | "fallback";

export function HeroLamp() {
  const [mode, setMode] = React.useState<Mode>("pending");
  const theme = useLampTheme();
  const switchRef = React.useRef<HTMLButtonElement>(null);

  useLayout(() => {
    hydrateFromStorage();
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    setMode(!reduce && hasWebGL() ? "scene" : "fallback");
    // The head script + the store set data-lb-theme for "/" only; other .ld-page
    // routes must never inherit it across a soft nav. Returning to "/" re-runs
    // hydrateFromStorage() and rewrites the current theme.
    return () => {
      document.documentElement.removeAttribute("data-lb-theme");
      // ...and the strike marker with it, or the next route runs every hover
      // and focus at the strike's 0.7s for the rest of the 800ms window.
      clearTheming();
    };
  }, []);

  if (mode === "fallback") return <HangingLamp />;

  return (
    <>
      {/* `pending` deliberately draws no lamp: swapping an SVG lamp for a 3D
          one a frame later is a worse first impression than an unlit room the
          lamp arrives into. Scripts off is the one case that never resolves,
          so <noscript> gets the SVG — its effects cannot run there anyway. */}
      {mode === "scene" && (
        <>
          <Scene switchRef={switchRef} />
          {/* Scoped to the scene: the SVG fallback carries its own control, and
              scripts-off would otherwise get this inert button beside the
              <noscript> lamp's — two "Turn on the light"s, neither wired up. */}
          <button
            ref={switchRef}
            type="button"
            className="ld-lamp-switch"
            aria-label={theme === "dark" ? "Turn on the light" : "Turn off the light"}
            onClick={strike}
          />
        </>
      )}
      <noscript>
        <HangingLamp />
      </noscript>
    </>
  );
}

export default HeroLamp;
