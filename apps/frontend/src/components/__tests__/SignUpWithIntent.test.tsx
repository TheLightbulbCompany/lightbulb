// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("@clerk/nextjs", () => ({
  SignUp: (props: {
    fallbackRedirectUrl?: string;
    signInFallbackRedirectUrl?: string;
  }) => (
    <div
      data-testid="sign-up-mock"
      data-fallback-redirect-url={props.fallbackRedirectUrl}
      data-sign-in-fallback-redirect-url={props.signInFallbackRedirectUrl}
    />
  ),
}));

import { SignUpWithIntent } from "../SignUpWithIntent";
import { SIGNUP_INTENT_COOKIE } from "@/lib/signupIntent";

describe("SignUpWithIntent", () => {
  beforeEach(() => {
    document.cookie = `${SIGNUP_INTENT_COOKIE}=; max-age=0; path=/`;
    window.history.replaceState({}, "", "/sign-up");
  });

  it("routes to /onboarding?intent=team via the ?intent=team query alone", () => {
    window.history.replaceState({}, "", "/sign-up?intent=team");
    render(<SignUpWithIntent />);
    const el = screen.getByTestId("sign-up-mock");
    expect(el.dataset.fallbackRedirectUrl).toBe("/onboarding?intent=team");
  });

  it("routes to /onboarding?intent=team via the cookie alone (Clerk's multi-step signup already dropped the query)", () => {
    document.cookie = `${SIGNUP_INTENT_COOKIE}=team; path=/`;
    // A Clerk sub-route, e.g. after the query-carrying /sign-up?intent=team
    // hop, reached during email verification — no query here, cookie only.
    window.history.replaceState({}, "", "/sign-up/verify-email-address");
    render(<SignUpWithIntent />);
    const el = screen.getByTestId("sign-up-mock");
    expect(el.dataset.fallbackRedirectUrl).toBe("/onboarding?intent=team");
  });

  it("routes to /chat when there is no team intent, unchanged from before", () => {
    render(<SignUpWithIntent />);
    const el = screen.getByTestId("sign-up-mock");
    expect(el.dataset.fallbackRedirectUrl).toBe("/chat");
    expect(el.dataset.signInFallbackRedirectUrl).toBe("/chat");
  });

  it("regression guard: signInFallbackRedirectUrl stays /chat even when team intent is present", () => {
    // signInFallbackRedirectUrl fires on Clerk's own "Sign in" footer link —
    // an EXISTING account, not the fresh-signup double-provision case this
    // component exists to fix. Routing it to /onboarding?intent=team would
    // let an existing org owner land back on unguarded org-creation UI and
    // create a second org/container. Must never re-couple to redirectTarget.
    document.cookie = `${SIGNUP_INTENT_COOKIE}=team; path=/`;
    window.history.replaceState({}, "", "/sign-up?intent=team");
    render(<SignUpWithIntent />);
    const el = screen.getByTestId("sign-up-mock");
    expect(el.dataset.fallbackRedirectUrl).toBe("/onboarding?intent=team");
    expect(el.dataset.signInFallbackRedirectUrl).toBe("/chat");
  });
});
