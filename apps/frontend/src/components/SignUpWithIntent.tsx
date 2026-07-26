"use client";

import { useState } from "react";
import { SignUp } from "@clerk/nextjs";

import { resolveSignUpRedirectTarget } from "@/lib/signupIntent";

/**
 * `<SignUp>` with a redirect target that branches on team intent instead of
 * always landing on /chat.
 *
 * Why this exists: a static `fallbackRedirectUrl="/chat"` sent every signup
 * — including team ones — through /chat first. /chat's ProvisioningStepper
 * polls GET /container/status immediately on mount, which provisions a
 * throwaway personal container + trial_local billing row + Paperclip company
 * before useOnboardingGate ever got a chance to notice the team intent and
 * redirect to /onboarding. By the time the user created their org, they had
 * two containers.
 *
 * The fix: compute the redirect target once, synchronously, from the current
 * query string and signup-intent cookie (see `lib/signupIntent.ts` for why
 * both are read — the cookie is what survives Clerk's multi-step signup).
 * `useState`'s lazy initializer runs during the component's first render, so
 * the value is correct on first paint — no effect-timing gap where a stale
 * default could leak through to Clerk.
 *
 * Guarded for SSR (`window`/`document` are undefined during the server
 * render): default to "/chat" there. Safe because this only matters up to
 * the point the user actually completes sign-up, well after hydration has
 * recomputed the real value client-side.
 *
 * `signInFallbackRedirectUrl` stays a static "/chat", NOT the computed team
 * target. That prop only fires when the visitor uses the widget's own
 * "Sign in" footer link to an EXISTING account — never the fresh-signup
 * double-provision case this component exists to fix. Routing it to
 * /onboarding?intent=team would let an existing org owner (who clicked
 * "Create a team", wrote the intent cookie, then remembered they already
 * have an account) land back on the org-creation UI with no membership
 * guard, creating a second org and a second container.
 */
export function SignUpWithIntent() {
  const [redirectTarget] = useState<string>(() =>
    typeof window === "undefined"
      ? "/chat"
      : resolveSignUpRedirectTarget(window.location.search, document.cookie),
  );

  return (
    <SignUp
      fallbackRedirectUrl={redirectTarget}
      signInFallbackRedirectUrl="/chat"
    />
  );
}
