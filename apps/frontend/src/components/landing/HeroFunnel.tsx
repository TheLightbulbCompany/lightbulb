"use client";

/**
 * The landing hero IS the first-run funnel: the okara-style variant of the
 * in-chat FirstRunSteps flow — one pill input with the CTA embedded, and the
 * two paths as a pair of doors above it, equal weight. Same {path, input,
 * detail} model; the textarea layout stays in-chat.
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
import { captureSignupCta } from "@/lib/signupFlow";
import { HeroLamp } from "./HeroLamp";
import { lightOn } from "./HeroLamp/lampStore";

// Same two paths and the same words as the in-chat picker. Grow is listed
// first and pre-selected: every paying owner so far arrived already running
// something. Reorder to flip the default.
const DOORS: { path: Path; label: string }[] = [
  { path: "grow", label: "Grow my business" },
  { path: "new", label: "Start something new" },
];

const COPY: Record<Path, { placeholder: string; aria: string }> = {
  grow: { placeholder: "yourcompany.com", aria: "Your company's website" },
  new: { placeholder: "A ceramics studio that ships nationwide…", aria: "Your idea" },
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
  const [path, setPath] = React.useState<Path>(DOORS[0].path);
  const [input, setInput] = React.useState("");

  // 3..2000 mirrors the backend BriefBody rules — an oversized stash would be
  // rejected by the replay hook AFTER signup, silently losing the brief.
  const canSubmit = input.trim().length >= 3 && input.trim().length <= 2000;

  // Launch sheet open: the same funnel, framed as joining the launch. The
  // stash → /sign-up flow is unchanged — the sheet itself is joined
  // in-product, after onboarding.
  const launchFraming = launch?.phase === "open";

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    // This CTA is explicitly personal: a leftover team-intent cookie (30-min
    // TTL from the pricing Teams CTA) would route the signup into team
    // creation and strand the stashed brief in an org context.
    clearSignupIntentCookie();
    stash(path, input.trim());
    captureSignupCta({
      ctaId: "hero_funnel_submit",
      label: launchFraming ? "Join the launch" : "Start",
      surface: "landing_hero_funnel",
      // new-business vs grow-existing — the one signal that separates the
      // two framings this hero sells, so it must survive the event rename.
      properties: { path },
    });
    router.push("/sign-up");
  };

  return (
    <section className="ld-funnel" id="start">
      <HeroLamp />
      <div className="ld-funnel-in">
        <LaunchHeroPill />
        <h1 className="ld-funnel-h1">
          AI employees for{" "}
          <br />
          small teams &amp; solo founders.
        </h1>

        <div className="ld-doors" role="group" aria-label="What are you bringing?">
          {DOORS.map((door) => (
            <button
              key={door.path}
              type="button"
              className="ld-door"
              aria-pressed={path === door.path}
              onClick={() => {
                setPath(door.path);
                stash(door.path, input);
              }}
            >
              {door.label}
            </button>
          ))}
        </div>

        <form className="ld-ask" onSubmit={submit}>
          <input
            maxLength={2000}
            value={input}
            onChange={(e) => {
              const next = e.target.value;
              // The first character IS the lightbulb moment. Only ever
              // lights, never darkens; the lamp itself is the toggle.
              if (next.length > 0) lightOn();
              setInput(next);
              stash(path, next);
            }}
            placeholder={COPY[path].placeholder}
            aria-label={COPY[path].aria}
          />
          <button type="submit" className="ld-btn" disabled={!canSubmit}>
            {launchFraming ? "Join the launch" : "Start"}{" "}
            <span className="ld-btn-arrow" aria-hidden="true">→</span>
          </button>
        </form>

        {/* What the reader gets, not what the product is: the deliverables of
            a real first run, then the work that stops being theirs. The second
            sentence is the one that finishes "this saves me ___" — the H1 now
            says who it's for and what they are, so this line has to say what
            comes off the reader's plate, not re-introduce the roster. */}
        <p className="ld-funnel-fine">
          Five minutes in, your CEO has read your market, filed the first three
          moves, and set up a working email address. It answers the inbox,
          chases the follow-ups, and hires specialists as the work grows — you
          approve, they do it.
        </p>
        {/* The price is the first question every visitor has, and until this
            line the only answer above the fold was a 64px "$50" further down
            the page. Free-to-try is the strongest thing we offer and it was
            invisible: owners were arriving in the product believing they had
            already been charged. The three ways to pay for inference are the
            one line no competitor can write; they were buried in pricing. */}
        <p className="ld-funnel-fine">
          <strong>Free for 3 days. No credit card.</strong> Runs on your ChatGPT
          login, your own API key, or Claude credits. $50 a month, flat.
        </p>
      </div>
    </section>
  );
}
