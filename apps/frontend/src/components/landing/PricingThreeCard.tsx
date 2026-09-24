"use client";
import Image from "next/image";
import Link from "next/link";

import { PROVIDER_CARDS } from "@/lib/providerCards";
import { captureSignupCta } from "@/lib/signupFlow";

import { Arrow } from "./Arrow";

/**
 * Pricing — one workspace, three ways to power inference.
 *
 * The cards are provider PATHS, not plans. Exact plan prices (Base/Workspace/
 * Ultra) live on the in-app Billing page via `/billing/account` — never
 * hardcoded here. Order and highlighting come from `PROVIDER_CARDS`.
 */
const ART: Record<string, { src: string; pos: string }> = {
  chatgpt_oauth: { src: "/landing/ladder-star.jpg", pos: "center 30%" },
  byo_key: { src: "/landing/step-work.jpg", pos: "center 40%" },
  openrouter_managed: { src: "/landing/plan-lights.jpg", pos: "center 34%" },
};

function Tick() {
  return (
    <svg width="13" height="13" viewBox="0 0 14 14" aria-hidden="true">
      <path d="M2.5 7.3l3 3 6-6.2" fill="none" stroke="#B68235" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function PricingThreeCard() {
  return (
    <section id="pricing" className="lb-pricing">
      <div className="lb-wrap">
        <h2 className="lb-h2">
          One workspace.{" "}
          <br className="lb-brk" />
          Three ways to power it.
        </h2>

        <p className="lb-price-hero">
          <b>Choose your plan</b>
        </p>
        <p className="lb-price-free">
          <strong>Start free for 30 days</strong> — no credit card.
        </p>

        <div className="lb-plans">
          {PROVIDER_CARDS.map((card) => {
            const art = ART[card.id];
            return (
              <article key={card.id} className={`lb-plan${card.highlighted ? " rec" : ""}`}>
                <div className="lb-plan-art">
                  {art && (
                    <Image
                      src={art.src}
                      alt=""
                      fill
                      sizes="(max-width: 980px) 100vw, 33vw"
                      style={{ objectFit: "cover", objectPosition: art.pos }}
                    />
                  )}
                  <span className="lb-rec-pill">{card.highlighted ? "Recommended" : "Option"}</span>
                </div>
                <div className="lb-plan-b">
                  <h3>{card.title}</h3>
                  <p className="lb-blurb">{card.subtitle}</p>
                  <Link
                    href={card.href}
                    className={`lb-btn ${card.highlighted ? "lb-btn-p" : "lb-btn-s"}`}
                    onClick={() =>
                      captureSignupCta({
                        ctaId: `pricing_card_${card.id}`,
                        label: card.cta,
                        surface: "landing_pricing",
                        destination: "/sign-up",
                        providerChoice: card.id,
                      })
                    }
                  >
                    {card.cta}
                  </Link>
                  <ul className="lb-feats">
                    {card.bullets.map((b) => (
                      <li key={b}>
                        <Tick />
                        <span>{b}</span>
                      </li>
                    ))}
                  </ul>
                  <p className="lb-trial-note">{card.trialNote}</p>
                </div>
              </article>
            );
          })}
        </div>

        <div className="lb-ws-strip">
          <div>
            <h3>Your workspace</h3>
            <p>Agents, people, and work in one place.</p>
          </div>
          <Link
            href="/sign-up"
            className="lb-btn lb-btn-p"
            onClick={() =>
              captureSignupCta({
                ctaId: "pricing_create_workspace",
                label: "Create a workspace",
                surface: "landing_pricing",
                destination: "/sign-up",
              })
            }
          >
            Create a workspace
            <Arrow />
          </Link>
        </div>
      </div>
    </section>
  );
}

export default PricingThreeCard;
