"use client";

import Link from "next/link";
import { useEffect } from "react";

import { register } from "@/lib/analytics";
import { HEADLINE_COOKIE, readHeadlineVariantCookie, type HeadlineVariant } from "@/lib/headlineVariant";
import { captureSignupCta } from "@/lib/signupFlow";

import { Arrow } from "./Arrow";

const NINETY_DAYS_S = 60 * 60 * 24 * 90;

/** The hero's two doors, and the place a first visit keeps its headline variant. */
export function HeroCtas({ variant }: { variant: HeadlineVariant }) {
  useEffect(() => {
    if (!readHeadlineVariantCookie()) {
      document.cookie = `${HEADLINE_COOKIE}=${variant}; Path=/; Max-Age=${NINETY_DAYS_S}; SameSite=Lax`;
    }
    register({ headline_variant: variant });
  }, [variant]);

  const track = (ctaId: string, label: string, destination: string) => () =>
    captureSignupCta({ ctaId, label, surface: "hero", destination, properties: { headline_variant: variant } });

  return (
    <div className="lb-hero-cta">
      <div className="lb-cta-col">
        <Link
          href="/request-setup"
          className="lb-btn lb-btn-p lb-btn-xl"
          onClick={track("hero_setup", "Request a guided setup", "/request-setup")}
        >
          Request a guided setup
          <Arrow />
        </Link>
        <p className="lb-cta-note">For founding teams of 3 to 15. Ten spots. A founder replies within a day.</p>
      </div>
      <div className="lb-cta-col">
        <Link href="/sign-up" className="lb-btn lb-btn-s lb-btn-xl" onClick={track("hero_start_free", "Start free", "/sign-up")}>
          Start free
        </Link>
        <p className="lb-cta-note">30 days free. No card to start.</p>
      </div>
    </div>
  );
}
