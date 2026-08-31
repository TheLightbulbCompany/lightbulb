"use client";

/**
 * The landing hero IS the first-run funnel: the okara-style variant of the
 * in-chat FirstRunSteps flow (defaultPath="new") — one pill input with the
 * CTA embedded, new-business as the default, the grow swap as a quiet line
 * beneath. Same {path, input, detail} model; the two-path picker and the
 * textarea layout stay in-chat.
 *
 * Answers persist to sessionStorage under LANDING_BRIEF_STORAGE_KEY on every
 * change; Start stashes and routes into Clerk signup (same tab), where
 * useLandingBriefReplay replays the brief after auth. Personal flow only —
 * the team-intent cookie belongs to PricingThreeCard and is never set here.
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import type { Path } from "@/components/onboarding/FirstRunSteps";
import { LaunchHeroPill } from "@/components/launch/LaunchHeroPill";
import { useLaunchInfo } from "@/components/launch/useLaunchInfo";
import { LANDING_BRIEF_STORAGE_KEY } from "@/hooks/useLandingBriefReplay";
import { clearSignupIntentCookie } from "@/lib/signupIntent";
import { capture } from "@/lib/analytics";
import { HeroLamp } from "./HeroLamp";
import { lightOn } from "./HeroLamp/lampStore";

const COPY: Record<Path, { placeholder: string; swap: string }> = {
  new: {
    placeholder: "A ceramics studio that ships nationwide…",
    swap: "Already have a business? Grow it instead",
  },
  grow: {
    placeholder: "yourcompany.com",
    swap: "Starting from scratch? Describe your idea instead",
  },
};

function stash(path: Path, input: string) {
  try {
    window.sessionStorage.setItem(
      LANDING_BRIEF_STORAGE_KEY,
      JSON.stringify({ path, input, detail: "" }),
    );
  } catch {
    // Storage unavailable (private mode) — the in-chat funnel is the fallback.
  }
}

export function HeroFunnel() {
  const router = useRouter();
  const launch = useLaunchInfo();
  const [path, setPath] = React.useState<Path>("new");
  const [input, setInput] = React.useState("");

  // 3..2000 mirrors the backend BriefBody rules — an oversized stash would be
  // rejected by the replay hook AFTER signup, silently losing the brief.
  const canSubmit = input.trim().length >= 3 && input.trim().length <= 2000;

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    // This CTA is explicitly personal: a leftover team-intent cookie (30-min
    // TTL from the pricing Teams CTA) would route the signup into team
    // creation and strand the stashed brief in an org context.
    clearSignupIntentCookie();
    stash(path, input.trim());
    capture("signup_started", {
      source: "landing_hero_funnel",
      path,
      destination: "/sign-up",
    });
    router.push("/sign-up");
  };

  // Launch sheet open: the same funnel, framed as joining the launch. The
  // stash → /sign-up flow is unchanged — the sheet itself is joined
  // in-product, after onboarding.
  const launchFraming = launch?.phase === "waitlist" || launch?.phase === "activation";

  return (
    <section className="ld-funnel" id="start">
      <HeroLamp />
      <div className="ld-funnel-in">
        <p className="ld-mono ld-funnel-eyebrow">
          The Integrated Management Environment
        </p>
        <LaunchHeroPill />
        <h1 className="ld-funnel-h1">
          Type an idea.{" "}
          <br />
          Watch it become a company.
        </h1>

        <form className="ld-ask" onSubmit={submit}>
          <input
            maxLength={2000}
            value={input}
            onChange={(e) => {
              const next = e.target.value;
              // "Type an idea. Watch it become a company." — the first
              // character IS the lightbulb moment. Only ever lights, never
              // darkens; the lamp itself is the toggle.
              if (next.length > 0) lightOn();
              setInput(next);
              stash(path, next);
            }}
            placeholder={COPY[path].placeholder}
            aria-label={
              path === "new" ? "Your idea" : "Your company's website"
            }
          />
          <button type="submit" className="ld-btn" disabled={!canSubmit}>
            {launchFraming ? "Join the launch" : "Start"}{" "}
            <span className="ld-btn-arrow" aria-hidden="true">→</span>
          </button>
        </form>

        <p className="ld-funnel-fine">
          Your company gets a name, a working email address, and its first
          three moves — in its own persistent, isolated container.
        </p>
        <button
          type="button"
          className="ld-funnel-swap"
          onClick={() => {
            const next: Path = path === "new" ? "grow" : "new";
            setPath(next);
            stash(next, input);
          }}
        >
          {COPY[path].swap}
        </button>
      </div>
    </section>
  );
}
