"use client";
import Link from "next/link";

import { PROVIDER_CARDS } from "@/lib/providerCards";

export function PricingThreeCard() {
  return (
    <section id="pricing" className="ld-pricing">
      <div className="ld-section">
        <div className="ld-kicker" data-ld-reveal>
          pricing
          <span className="ld-kicker-index">06 — 08</span>
        </div>
        <h2 className="ld-h2" data-ld-reveal>
          One price. <em>Three ways to power it.</em>
        </h2>
        <p className="ld-lede" data-ld-reveal>
          Every plan is the same always-on agent infrastructure — your pod,
          your team, every channel. Choose how you want to pay for inference.
        </p>
        <div className="ld-price-note" data-ld-reveal>
          <span className="ld-price-big">
            <sup>$</sup>50
          </span>
          <span className="ld-price-per">/ month · flat</span>
        </div>

        <div className="ld-price-grid" data-ld-reveal-group>
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
              <Link href={card.href} className="ld-btn">
                {card.cta}
                <span className="ld-btn-arrow" aria-hidden="true">
                  →
                </span>
              </Link>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

export default PricingThreeCard;
