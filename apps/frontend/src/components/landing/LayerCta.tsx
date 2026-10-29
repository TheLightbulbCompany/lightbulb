"use client";

import Link from "next/link";

import { captureSignupCta } from "@/lib/signupFlow";

import { Arrow } from "./Arrow";

/** The workspace card's door in the two-layer band; same event as the hero's. */
export function LayerCta() {
  return (
    <Link
      href="/sign-up"
      className="lb-btn lb-btn-p"
      onClick={() =>
        captureSignupCta({ ctaId: "layers_start_free", label: "Start your workspace free", surface: "layers", destination: "/sign-up" })
      }
    >
      Start your workspace free
      <Arrow />
    </Link>
  );
}
