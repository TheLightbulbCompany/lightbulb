/**
 * Team-signup intent transport.
 *
 * The landing "Create a team" CTA must get an `intent=team` signal all the way
 * to /onboarding. Relying on the `?intent=team` query surviving Clerk's
 * multi-step signup flow (email verification, SSO callback — each a distinct
 * sub-route that does NOT carry the original query) is unreliable. So the CTA
 * writes a short-lived cookie that survives every sub-route and the final
 * redirect; useOnboardingGate consumes it once on /chat and forwards
 * `?intent=team` to /onboarding, then clears it.
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
