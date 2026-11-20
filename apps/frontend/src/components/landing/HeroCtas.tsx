"use client";

import Link from "next/link";

import { captureSignupCta } from "@/lib/signupFlow";

import { Arrow } from "./Arrow";

const TRUST = ["Keep your number", "Month to month", "English and Spanish"] as const;

/**
 * The hero's doors: sign-up is the one primary action; "Hear the voices" is a
 * quieter second that scrolls to the stock samples. The trust row sits under them.
 */
export function HeroCtas() {
  return (
    <>
      <div className="lb-hero-cta">
        <Link
          href="/sign-up"
          className="lb-btn lb-btn-p lb-btn-xl"
          onClick={() =>
            captureSignupCta({
              ctaId: "hero_start_free",
              label: "Set up your assistant",
              surface: "hero",
              destination: "/sign-up",
            })
          }
        >
          Set up your assistant
          <Arrow />
        </Link>
        <a href="#voices" className="lb-btn lb-btn-s lb-btn-xl">
          Hear the voices
        </a>
      </div>
      <ul className="lb-ticks">
        {TRUST.map((t) => (
          <li key={t} className="lb-tick">
            <i>
              <Tick />
            </i>
            {t}
          </li>
        ))}
      </ul>
    </>
  );
}

/** The company home's doors: set up Phone, or talk to a founder. */
export function HubCtas() {
  return (
    <div className="lb-hero-cta">
      <Link
        href="/sign-up"
        className="lb-btn lb-btn-p lb-btn-xl"
        onClick={() =>
          captureSignupCta({
            ctaId: "hub_sign_up",
            label: "Set up Lightbulb Phone",
            surface: "hero",
            destination: "/sign-up",
          })
        }
      >
        Set up Lightbulb Phone
        <Arrow />
      </Link>
      <Link href="/request-setup" className="lb-btn lb-btn-s lb-btn-xl">
        Talk to a founder
      </Link>
    </div>
  );
}

function Tick() {
  return (
    <svg width="9" height="9" viewBox="0 0 10 10" aria-hidden="true">
      <path
        d="M2 5l2 2 4-4"
        fill="none"
        stroke="#B68235"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
