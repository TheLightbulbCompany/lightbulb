/**
 * Team-signup intent transport.
 *
 * The landing "Create a team" CTA must get an `intent=team` signal all the way
 * to /onboarding. Relying on the `?intent=team` query surviving Clerk's
 * multi-step signup flow (email verification, SSO callback — each a distinct
 * sub-route that does NOT carry the original query) is unreliable. So the CTA
 * writes a short-lived cookie that survives every sub-route and the final
 * redirect.
 *
 * Two consumers, one cookie:
 *   - SignUpWithIntent (components/SignUpWithIntent.tsx) reads it on /sign-up
 *     to pick Clerk's post-signup redirect target, so a team signup lands on
 *     /onboarding?intent=team directly and never mounts /chat (which would
 *     otherwise provision a throwaway personal container via the
 *     GET /container/status poll before the team intent was ever noticed).
 *     /onboarding clears the cookie once it renders the org-creation UI off
 *     of it.
 *   - useOnboardingGate consumes it on /chat for the in-app "Create a team"
 *     flow (an existing user who wasn't on a fresh /sign-up), forwards
 *     `?intent=team` to /onboarding, and clears it there.
 * Clearing is idempotent, so whichever consumer gets there first is fine.
 */
export const SIGNUP_INTENT_COOKIE = "isol8_signup_intent";

/** Parse the signup-intent value out of a `document.cookie` string. Pure. */
export function readSignupIntentCookie(cookieString: string): string | null {
  for (const part of cookieString.split(";")) {
    const trimmed = part.trim();
    if (!trimmed) continue;
    const eq = trimmed.indexOf("=");
    if (eq === -1) continue;
    if (trimmed.slice(0, eq) === SIGNUP_INTENT_COOKIE) {
      return decodeURIComponent(trimmed.slice(eq + 1));
    }
  }
  return null;
}

/** Set the signup-intent cookie (client-side; ~30-min TTL as a staleness cap). */
export function writeSignupIntentCookie(value: string): void {
  document.cookie = `${SIGNUP_INTENT_COOKIE}=${encodeURIComponent(value)}; path=/; max-age=1800; samesite=lax`;
}

/** Clear the signup-intent cookie once it has been consumed. */
export function clearSignupIntentCookie(): void {
  document.cookie = `${SIGNUP_INTENT_COOKIE}=; path=/; max-age=0; samesite=lax`;
}

/**
 * Where a completed /sign-up should land, given the current query string and
 * cookie jar. Team intent (query param OR cookie — the cookie is the durable
 * signal because Clerk's multi-step signup drops the query on sub-routes like
 * /sign-up/verify-email-address) sends the user straight to org creation so
 * /chat — and the personal-container provisioning poll it mounts — is never
 * reached for a team signup. Pure so it's testable without jsdom/window.
 */
export function resolveSignUpRedirectTarget(
  search: string,
  cookieString: string,
): string {
  const params = new URLSearchParams(search);
  const hasTeamIntent =
    params.get("intent") === "team" || readSignupIntentCookie(cookieString) === "team";
  return hasTeamIntent ? "/onboarding?intent=team" : "/chat";
}
