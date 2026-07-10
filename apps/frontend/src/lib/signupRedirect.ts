/**
 * Post-signup redirect target for Clerk's <SignUp>.
 *
 * The landing "Create a team" CTA links to /sign-up?intent=team. Clerk's
 * multi-step signup flow (email verification, etc.) does NOT reliably carry an
 * arbitrary ?intent= query param through to its post-signup redirect — the
 * final redirect uses the configured fallbackRedirectUrl. So we bake the team
 * signal into that redirect URL explicitly rather than relying on query
 * preservation. useOnboardingGate then forwards ?intent=team from /chat to
 * /onboarding, where the org-creation flow renders.
 */
export function signupRedirectUrl(intent: string | undefined): string {
  return intent === "team" ? "/chat?intent=team" : "/chat";
}
