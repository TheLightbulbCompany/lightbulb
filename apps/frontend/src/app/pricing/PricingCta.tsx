"use client";

import Link from "next/link";

import { captureSignupCta } from "@/lib/signupFlow";

/** The /pricing page's one button, tracked like every other sign-up door. */
export function PricingCta() {
  return (
    <Link
      href="/sign-up"
      className="editorial-btn editorial-btn--primary mt-10 h-11 px-6"
      onClick={() =>
        captureSignupCta({
          ctaId: "pricing_set_up_assistant",
          label: "Set up your assistant",
          surface: "pricing_page",
          destination: "/sign-up",
        })
      }
    >
      Set up your assistant
    </Link>
  );
}
