"use client";

import { useEffect, useRef } from "react";
import { useLenis } from "lenis/react";
import gsap from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";

gsap.registerPlugin(ScrollTrigger);

declare global {
  interface Window {
    __ldGsap?: typeof gsap;
    __ldST?: typeof ScrollTrigger;
  }
}

// Dev-only handle so animation states can be driven deterministically from
// the console / automated visual tests (rAF is suspended in hidden tabs).
if (typeof window !== "undefined" && process.env.NODE_ENV === "development") {
  window.__ldGsap = gsap;
  window.__ldST = ScrollTrigger;
}

/**
 * Landing-wide motion conductor.
 *
 * Owns the pieces of motion that belong to the page rather than any one
 * section: Lenis <-> ScrollTrigger sync, the top progress bar, and the
 * generic [data-ld-reveal] / [data-ld-reveal-group] scroll reveals that
 * every section opts into via markup. Section-specific timelines (hero
 * intro, pinned how-it-works) live with their components.
 *
 * Hidden states are applied by GSAP at runtime — never in CSS — so the
 * page renders complete without JavaScript.
 */
export function ScrollManager() {
  const progressRef = useRef<HTMLDivElement>(null);
  const lenis = useLenis();

  // Keep ScrollTrigger in lockstep with Lenis' smoothed scroll position.
  useEffect(() => {
    if (!lenis) return;
    const update = () => ScrollTrigger.update();
    lenis.on("scroll", update);
    return () => lenis.off("scroll", update);
  }, [lenis]);

  useEffect(() => {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    const ctx = gsap.context(() => {
      if (progressRef.current) {
        gsap.to(progressRef.current, {
          scaleX: 1,
          ease: "none",
          scrollTrigger: { start: 0, end: "max", scrub: 0.3 },
        });
      }

      if (reduced) return;

      gsap.utils.toArray<HTMLElement>("[data-ld-reveal]").forEach((el) => {
        gsap.fromTo(
          el,
          { autoAlpha: 0, y: 36 },
          {
            autoAlpha: 1,
            y: 0,
            duration: 1,
            ease: "power3.out",
            scrollTrigger: { trigger: el, start: "top 86%", once: true },
          }
        );
      });

      gsap.utils.toArray<HTMLElement>("[data-ld-reveal-group]").forEach((group) => {
        const children = Array.from(group.children);
        if (!children.length) return;
        gsap.fromTo(
          children,
          { autoAlpha: 0, y: 30 },
          {
            autoAlpha: 1,
            y: 0,
            duration: 0.9,
            ease: "power3.out",
            stagger: 0.12,
            scrollTrigger: { trigger: group, start: "top 84%", once: true },
          }
        );
      });
    });

    // Layout shifts after webfonts land; recompute trigger positions.
    document.fonts?.ready.then(() => ScrollTrigger.refresh()).catch(() => {});

    console.log(
      "%c🏝️ isol8 %c— Your private AI pod",
      "font-size:20px;font-weight:900;color:#06402B;",
      "font-size:14px;color:#888;"
    );
    console.log(
      "%cCurious? We like that. → https://isol8.co",
      "font-size:11px;color:#4a9e74;"
    );

    return () => ctx.revert();
  }, []);

  return <div className="ld-progress" ref={progressRef} />;
}
