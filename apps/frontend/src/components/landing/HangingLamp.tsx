"use client";

/**
 * The hanging lamp — the landing page's one piece of theatre, and its
 * light/dark switch.
 *
 * It hangs OVER the page from the top of the viewport on a cord, and it is a
 * real pendulum: it has mass, it damps, and scrolling nudges it. Clicking the
 * glass (or pulling the brass bead) strikes the filament and floods the room.
 *
 * Scope: the shared store (`./HeroLamp/lampStore`) writes `data-lb-theme` on
 * <html> and persists to localStorage; this component only reads it. Only
 * `.ld-page` reads the attribute, so this cannot leak into /chat — the app
 * has its own surface contract and is not themed by this control.
 *
 * prefers-reduced-motion: no pendulum, no scroll nudge, no circular flood.
 * The theme still toggles; it just cross-fades.
 *
 * Initial state is dark — the room starts unlit; typing an idea or clicking
 * the glass lights it.
 */

import * as React from "react";
import { hydrateFromStorage, strike, useLampTheme } from "./HeroLamp/lampStore";
import { SCROLL_IMPULSE, createPendulum } from "./HeroLamp/pendulum";

const GLASS = "M 86,150 L 72,132 L 72,76 A 28,32 0 0 1 128,76 L 128,132 L 114,150 Z";

export function HangingLamp() {
  const hostRef = React.useRef<HTMLDivElement>(null);
  const glassRef = React.useRef<SVGPathElement>(null);
  const theme = useLampTheme();

  // Adopt a stored choice after hydration. The server renders dark — the
  // room starts unlit — so storage is never read during render.
  React.useEffect(() => {
    hydrateFromStorage();
    return () => document.documentElement.removeAttribute("data-lb-theme");
  }, []);

  // Pendulum. transform only — never a layout property.
  React.useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    const pendulum = createPendulum();
    let raf = 0;
    let lastY = window.scrollY;

    const frame = () => {
      pendulum.step();
      host.style.transform = `rotate(${pendulum.angle}rad)`;
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);

    const onScroll = () => {
      const delta = window.scrollY - lastY;
      lastY = window.scrollY;
      pendulum.impulse(delta * SCROLL_IMPULSE);
    };
    window.addEventListener("scroll", onScroll, { passive: true });

    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("scroll", onScroll);
    };
  }, []);

  return (
    <div className="ld-lamp" ref={hostRef}>
      {/* NOT aria-hidden: the glass below is the page's light switch, and
          aria-hidden on this element would strip it from the a11y tree
          along with the decoration. Decorative parts are marked instead. */}
      <div className="ld-lamp-glow" aria-hidden="true" />
      <svg viewBox="0 0 290 300" className="ld-lamp-svg" focusable="false">
        <path className="ld-lamp-cord" d="M 145,0 L 145,120" aria-hidden="true" />
        {/* 90, not 45: the glass is drawn about local x=100, so at scale 0.55
            a 45 offset centres it on x=100 while the cord hangs at x=145 —
            the bulb rendered ~48px beside its own wire. 145 - 100*0.55 = 90. */}
        <g transform="translate(90 84) scale(0.55)">
          <path
            ref={glassRef}
            className="ld-lamp-glass"
            d={GLASS}
            role="button"
            tabIndex={0}
            aria-label={theme === "dark" ? "Turn on the light" : "Turn off the light"}
            onClick={strike}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                strike();
              }
            }}
          />
          <g className="ld-lamp-fil" aria-hidden="true">
            <path d="M 87,74 L 95,74 L 101,146 L 99,146 Z" />
            <path d="M 96,74 L 104,74 L 101.4,146 L 98.6,146 Z" />
            <path d="M 105,74 L 113,74 L 101,146 L 99,146 Z" />
            <path d="M 74,96 H 126 M 82,120 H 118" fill="none" />
          </g>
          <g className="ld-lamp-collar" aria-hidden="true">
            <rect x="86" y="150" width="28" height="7" rx="1" />
            <rect x="87" y="160" width="26" height="7" rx="1" />
            <rect x="89" y="170" width="22" height="6" rx="1" />
          </g>
          {/* the pull chain — a second, larger hit target for the same action */}
          <g className="ld-lamp-pull" onClick={strike}>
            <path className="ld-lamp-cord" d="M 112,176 L 112,224" />
            <circle cx="112" cy="230" r="7" />
            <rect x="96" y="168" width="32" height="72" fill="transparent" />
          </g>
        </g>
      </svg>
    </div>
  );
}

export default HangingLamp;
