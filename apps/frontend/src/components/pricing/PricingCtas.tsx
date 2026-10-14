"use client";

import Link from "next/link";

import { captureSignupCta } from "@/lib/signupFlow";

/** The /pricing buttons, tracked like every other sign-up door. */
export function PricingCtas() {
  return (
    <div className="mt-10 flex flex-wrap gap-4">
      <Link
        href="/request-setup"
        className="editorial-btn editorial-btn--primary h-11 px-6"
        onClick={() =>
          captureSignupCta({
            ctaId: "pricing_request_setup",
            label: "Request a guided setup",
            surface: "pricing_page",
            destination: "/request-setup",
          })
        }
      >
        Request a guided setup
      </Link>
      <Link
        href="/sign-up"
        className="editorial-btn h-11 px-6"
        onClick={() =>
          captureSignupCta({ ctaId: "pricing_start_free", label: "Start free", surface: "pricing_page", destination: "/sign-up" })
        }
      >
        Start free
      </Link>
    </div>
  );
}
