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
 *
 * The cookie has a second reader: `useProvisioningState` suppresses the
 * `GET /container/status` poll while it is set and no org is active yet. That
 * poll is what provisions, so leaving it running for the handful of renders
 * before the gate's redirect lands hands a team signup a throwaway PERSONAL
 * container + trial_local billing row + Paperclip company. Both readers go
 * through `hasTeamSignupIntentCookie` below — do not add a third open-coded
 * `readSignupIntentCookie(...) === "team"`.
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
 * Does this browser currently carry an unconsumed team-signup intent?
 *
 * The single team-intent predicate, shared by both readers (see the module
 * doc). TOTAL by construction: no `document` (SSR / prerender) and ANY failure
 * reading or decoding the cookie both answer `false`.
 *
 * That `false` direction is deliberate, not incidental. Both callers must FAIL
 * OPEN — `useOnboardingGate` falls back to treating the signup as personal
 * (its pre-cookie behaviour), and `useProvisioningState` falls back to
 * POLLING. A user must never be left unable to provision because a cookie read
 * threw, and `readSignupIntentCookie`'s `decodeURIComponent` throws on a value
 * a hand-edited or truncated cookie can easily hold (`...=%`), during render.
 */
export function hasTeamSignupIntentCookie(): boolean {
  if (typeof document === "undefined") return false;
  try {
    return readSignupIntentCookie(document.cookie) === "team";
  } catch {
    return false;
  }
}
