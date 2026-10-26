"use client";

import Link from "next/link";
import { useEffect } from "react";

import { register } from "@/lib/analytics";
import { headlineVariantProps, type HeadlineVariant } from "@/lib/headlineVariant";
import { captureSignupCta } from "@/lib/signupFlow";

import { Arrow } from "./Arrow";

const TRUST = ["30-day free trial", "No credit card required", "2 minute setup"] as const;

/**
 * The hero's doors: self-serve is the one primary action, guided setup a quieter
 * second, and the trust row sits right under them. The cookie is set by Hero's
 * inline script; this re-registers it as a backstop.
 */
export function HeroCtas({ variant }: { variant: HeadlineVariant }) {
  useEffect(() => {
    register({ headline_variant: variant, ...headlineVariantProps() });
  }, [variant]);

  const track = (ctaId: string, label: string, destination: string) => () =>
    captureSignupCta({ ctaId, label, surface: "hero", destination, properties: { headline_variant: variant } });

  return (
    <>
      <div className="lb-hero-cta">
        <Link
          href="/sign-up"
          className="lb-btn lb-btn-p lb-btn-xl"
          onClick={track("hero_start_free", "Try Lightbulb free", "/sign-up")}
        >
          Try Lightbulb free
          <Arrow />
        </Link>
        <Link
          href="/request-setup"
          className="lb-btn lb-btn-s lb-btn-xl"
          onClick={track("hero_setup", "Request a guided setup", "/request-setup")}
        >
          Request a guided setup
        </Link>
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
