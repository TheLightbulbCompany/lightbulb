// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

// Wiring guard: /sign-up must render SignUpWithIntent, not a raw <SignUp>.
//
// Reverting the page to `<SignUp fallbackRedirectUrl="/chat" …/>` would
// restore the bug this branch fixes (team signups mount /chat and
// double-provision) while every other test in this change — SignUpWithIntent,
// signupIntent, onboarding/page — stays green, because none of them import
// this page. Stubbing SignUpWithIntent to a marker and asserting it renders
// on /sign-up is the cheapest test that fails on that regression.
vi.mock("@/components/SignUpWithIntent", () => ({
  SignUpWithIntent: () => <div data-testid="signup-with-intent-mock" />,
}));

import Page from "../page";

describe("/sign-up page", () => {
  it("renders SignUpWithIntent, not a raw Clerk <SignUp>", () => {
    render(<Page />);
    expect(screen.getByTestId("signup-with-intent-mock")).toBeInTheDocument();
  });
});
