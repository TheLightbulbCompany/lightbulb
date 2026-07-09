"use client";

import Link from "next/link";
import dynamic from "next/dynamic";
import { useEffect, useRef, useState } from "react";
import gsap from "gsap";

import { capture } from "@/lib/analytics";

const HeroPod = dynamic(() => import("./HeroPod").then((m) => m.HeroPod), {
  ssr: false,
});

type Arch = "aarch64" | "x64";

// Browsers on macOS don't expose CPU architecture directly. Chromium-based
// browsers offer it through navigator.userAgentData high-entropy hints
// ("arm" or "x86"); Safari/Firefox don't support UA-CH on macOS at all.
// We default to Apple Silicon (supermajority since 2020) and surface an
// "Intel Mac?" fallback link so the minority can self-select.
async function detectArch(): Promise<Arch> {
  const uaData = (
    navigator as Navigator & {
      userAgentData?: {
        getHighEntropyValues?: (
          hints: string[],
        ) => Promise<{ architecture?: string }>;
      };
    }
  ).userAgentData;
  if (!uaData?.getHighEntropyValues) return "aarch64";
  try {
    const v = await uaData.getHighEntropyValues(["architecture"]);
    return v.architecture === "x86" ? "x64" : "aarch64";
  } catch {
    return "aarch64";
  }
}

// One kind of thing only: the tools a pod connects to.
const TICKER_ITEMS = [
  "Gmail",
  "Slack",
  "Salesforce",
  "Notion",
  "Google Calendar",
  "Office 365",
  "WhatsApp",
  "Discord",
  "Telegram",
  "Stripe",
];

function TickerRun() {
  return (
    <span>
      {TICKER_ITEMS.map((item) => (
        <span key={item}>
          {item} <i>·</i>
        </span>
      ))}
    </span>
  );
}

export function Hero() {
  const [arch, setArch] = useState<Arch>("aarch64");
  const rootRef = useRef<HTMLElement>(null);

  useEffect(() => {
    let cancelled = false;
    detectArch().then((a) => {
      if (!cancelled) setArch(a);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // Entrance: masked headline lines rise, then the supporting layers fade
  // up in sequence. Skipped entirely under prefers-reduced-motion.
  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const ctx = gsap.context(() => {
      const tl = gsap.timeline({ defaults: { ease: "power4.out" } });
      tl.fromTo(
        ".ld-line-inner",
        { yPercent: 110 },
        { yPercent: 0, duration: 1.3, stagger: 0.14 },
        0.15
      )
        .fromTo(
          ".ld-hero-topline",
          { autoAlpha: 0, y: -14 },
          { autoAlpha: 1, y: 0, duration: 0.9 },
          0.5
        )
        .fromTo(
          [".ld-hero-sub", ".ld-hero-actions"],
          { autoAlpha: 0, y: 26 },
          { autoAlpha: 1, y: 0, duration: 1, stagger: 0.12 },
          0.75
        )
        .fromTo(
          ".ld-hero-foot",
          { autoAlpha: 0 },
          { autoAlpha: 1, duration: 1.1 },
          1.05
        );
    }, rootRef);
    return () => ctx.revert();
  }, []);

  return (
    <section className="ld-hero" ref={rootRef}>
      <HeroPod />

      <div className="ld-hero-inner">
        <div className="ld-hero-topline">
          <p className="ld-mono">
            <b>isol8</b> — private ai pods
          </p>
          <p className="ld-mono">always-on · isolated · yours</p>
        </div>

        <h1 className="ld-hero-h1">
          <span className="ld-line">
            <span className="ld-line-inner">Your AI workforce,</span>
          </span>
          <span className="ld-line">
            <span className="ld-line-inner">
              <em>isolated.</em>
            </span>
          </span>
        </h1>

        <div className="ld-hero-row">
          <p className="ld-hero-sub">
            One private pod, an entire team of agents. They connect to the
            tools you already use, run multi-step workflows on their own, and
            report back — led by <strong>Friday</strong>, your AI chief of
            staff. Every pod is <strong>sandboxed in the cloud</strong>,
            reaching only the tools you connect.
          </p>

          <div className="ld-hero-actions">
            <div className="ld-hero-ctas">
              <Link
                href="/chat"
                className="ld-btn"
                onClick={() =>
                  capture("landing_cta_clicked", {
                    cta_id: "hero_start_pod",
                    section: "hero",
                    destination: "/chat",
                  })
                }
              >
                Start your pod
                <span className="ld-btn-arrow" aria-hidden="true">
                  →
                </span>
              </Link>
              <Link href="#how" className="ld-btn ld-btn--ghost">
                See how it works
              </Link>
            </div>
            <p className="ld-hero-fine">
              14-day trial, card required ·{" "}
              <a
                href={`/api/download/desktop?arch=${arch}`}
                onClick={() => capture("landing_download_clicked", { arch })}
              >
                Download for Mac
              </a>
              {arch === "aarch64" && (
                <>
                  {" · "}
                  <a
                    href="/api/download/desktop?arch=x64"
                    onClick={() =>
                      capture("landing_download_clicked", { arch: "x64" })
                    }
                  >
                    Intel Mac?
                  </a>
                </>
              )}
            </p>
          </div>
        </div>
      </div>

      <div className="ld-hero-foot">
        <div className="ld-scroll-cue">
          <span className="ld-cue-line" />
          <span className="ld-mono">works with</span>
        </div>
        <div className="ld-ticker" aria-hidden="true">
          <div className="ld-ticker-track">
            <TickerRun />
            <TickerRun />
          </div>
        </div>
      </div>
    </section>
  );
}
