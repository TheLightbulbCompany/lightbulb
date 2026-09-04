"use client";
import Link from "next/link";

import { PROVIDER_CARDS } from "@/lib/providerCards";
import { captureSignupCta } from "@/lib/signupFlow";
import { writeSignupIntentCookie } from "@/lib/signupIntent";

export function PricingThreeCard() {
  return (
    <section id="pricing" className="ld-pricing">
      <div className="ld-section">
        <div className="ld-kicker">
          pricing
        </div>
        <h2 className="ld-h2">
          One price. <em>Three ways to power it.</em>
        </h2>
        <p className="ld-lede">
          Everything it takes to run an autonomous business — your pod, your
          team, every channel. Choose how you want to pay for inference.
        </p>
        <div className="ld-price-note">
          <span className="ld-price-big">
            <sup>$</sup>50
          </span>
          <span className="ld-price-per">
            / month · flat · after your free trial
          </span>
        </div>

        {/* The free tier used to be an 11px aside on the price line, while all
            three card CTAs below carry a ?provider= that SKIPS it and lands
            the visitor straight in Stripe Checkout. So the cheapest way in was
            reachable only by the one route that never mentioned it, and people
            reasonably concluded it costs $50 to try. This is the free path,
            stated once, at the size the claim deserves — and it deliberately
            links to a bare /sign-up with no provider attached. */}
        <p className="ld-price-free">
          <strong>Start free for 3 days — no credit card.</strong>{" "}
          <Link
            href="/sign-up"
            onClick={() => {
              captureSignupCta({
                ctaId: "pricing_free_try_it_first",
                label: "Try it first",
                surface: "landing_pricing",
              });
            }}
          >
            Try it first
          </Link>{" "}
          and pick how you pay later.
        </p>

        <div className="ld-price-grid">
          {PROVIDER_CARDS.map((card) => (
            <div
              key={card.id}
              className={
                "ld-price-card" + (card.highlighted ? " ld-price-card--hi" : "")
              }
            >
              <span className="ld-pc-tag">
                {card.highlighted ? "recommended" : "option"}
              </span>
              <h3>{card.title}</h3>
              <p className="ld-pc-sub">{card.subtitle}</p>
              <p className="ld-pc-trial">{card.trialNote}</p>
              {/* Says out loud what these three buttons actually do. Each one
                  carries a `?provider=` that skips the free tier and goes
                  straight to Stripe Checkout — which is the whole reason people
                  believe it costs $50 to try. Putting a free path above them
                  (see .ld-price-free) without labelling these would just make
                  the section contradict itself: "start free" directly over a
                  button that charges. The route itself is unchanged; this makes
                  the choice honest at the point of click. */}
              <p className="ld-pc-charge">Billed today — skips the free trial</p>
              <ul>
                {card.bullets.map((b) => (
                  <li key={b}>{b}</li>
                ))}
              </ul>
              <Link
                href={card.href}
                className="ld-btn"
                onClick={() => {
                  captureSignupCta({
                    ctaId: `pricing_card_${card.id}`,
                    label: card.cta,
                    surface: "landing_pricing",
                    destination: card.href,
                    providerChoice: card.id,
                  });
                }}
              >
                {card.cta}
                <span className="ld-btn-arrow" aria-hidden="true">
                  →
                </span>
              </Link>
            </div>
          ))}
        </div>

        <div className="ld-teams-strip">
          <div className="ld-teams-copy">
            <h3>Lightbulb for Teams</h3>
            <p>Shared agents, one workspace, per-seat pricing.</p>
          </div>
          <Link
            href="/sign-up?intent=team"
            className="ld-btn"
            onClick={() => {
              // Robust team-intent transport: the cookie survives Clerk's
              // multi-step signup (which drops the ?intent=team query); the
              // gate reads it on /chat and routes to team creation.
              writeSignupIntentCookie("team");
              captureSignupCta({
                ctaId: "pricing_create_a_team",
                label: "Create a team",
                surface: "landing_pricing",
                destination: "/sign-up?intent=team",
                providerChoice: "team",
              });
            }}
          >
            Create a team
            <span className="ld-btn-arrow" aria-hidden="true">
              →
            </span>
          </Link>
        </div>
      </div>
    </section>
  );
}

export default PricingThreeCard;
