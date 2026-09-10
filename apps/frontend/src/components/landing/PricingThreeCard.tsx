"use client";
import Link from "next/link";

import { PROVIDER_CARDS } from "@/lib/providerCards";
import { captureSignupCta } from "@/lib/signupFlow";

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
              <ul>
                {card.bullets.map((b) => (
                  <li key={b}>{b}</li>
                ))}
              </ul>
              <Link
                href="/sign-up"
                className="ld-btn"
                onClick={() => {
                  captureSignupCta({
                    ctaId: `pricing_card_${card.id}`,
                    label: card.cta,
                    surface: "landing_pricing",
                    destination: "/sign-up",
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
            <h3>Your workspace</h3>
            <p>Agents, people, and work in one place.</p>
          </div>
          <Link
            href="/sign-up"
            className="ld-btn"
            onClick={() => {
              captureSignupCta({
                ctaId: "pricing_create_workspace",
                label: "Create a workspace",
                surface: "landing_pricing",
                destination: "/sign-up",
              });
            }}
          >
            Create a workspace
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
