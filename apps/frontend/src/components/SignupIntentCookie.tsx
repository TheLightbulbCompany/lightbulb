"use client";

import { useEffect } from "react";

import { writeSignupIntentCookie } from "@/lib/signupIntent";

/**
 * Writes the team signup-intent cookie when /sign-up is opened directly with
 * ?intent=team — e.g. a shared, bookmarked, or external link where the landing
 * "Create a team" CTA's onClick never ran. Without this, such a signup would
 * lose the team signal (Clerk's multi-step flow drops the query) and complete
 * as personal.
 *
 * Reads window.location (not useSearchParams) so the otherwise-static sign-up
 * page doesn't incur a CSR-bailout Suspense requirement.
 */
export function SignupIntentCookie() {
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("intent") === "team") {
      writeSignupIntentCookie("team");
    }
  }, []);
  return null;
}
