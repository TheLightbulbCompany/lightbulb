"use client";

import Link from "next/link";

import { VOICE_PLAN_NEUTRAL, voicePlanLine, type Pricing } from "@/lib/pricing";
import { captureSignupCta } from "@/lib/signupFlow";

import { Arrow } from "./Arrow";

// The first line carries the plan's price, read from the billing API by the page.
const OFFER = [
  "Your price locked for two years",
  "A shared channel with us inside your workspace, and fixes within days",
  "A say in what we build next",
];

export function FoundingTeams({ pricing: p }: { pricing: Pricing | null }) {
  return (
    <section className="lb-founding" id="founding-teams">
      <div className="lb-wrap">
        <p className="lb-eyebrow">Founding teams</p>
        <h2 className="lb-h2">Ten teams. Set up with you, by a founder.</h2>
        <p className="lb-founding-body">
          In 45 minutes, one of us sits with you and does four things: connect one real tool, wire one real
          workflow, invite your second person, and answer your first approval card.
        </p>
        <ul className="lb-founding-list">
          {[
            voicePlanLine(p) ?? VOICE_PLAN_NEUTRAL,
            ...OFFER,
          ].map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
        <p className="lb-founding-note">No discounts and no coupons. The free month is the offer.</p>
        <p className="lb-founding-note">
          For teams of 3 to 15 already running two or more agents, such as Claude Code or Codex, for real work.
        </p>
        <Link
          href="/request-setup?source=home-founding"
          className="lb-btn lb-btn-p lb-btn-xl"
          onClick={() =>
            captureSignupCta({
              ctaId: "founding_setup",
              label: "Request a guided setup",
              surface: "founding_teams",
              destination: "/request-setup",
            })
          }
        >
          Request a guided setup
          <Arrow />
        </Link>
      </div>
    </section>
  );
}
