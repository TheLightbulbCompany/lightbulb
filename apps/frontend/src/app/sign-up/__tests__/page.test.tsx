// @vitest-environment jsdom
import { expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import Page from "../[[...sign-up]]/page";

const signUpProps = vi.hoisted(() => vi.fn());
vi.mock("@clerk/nextjs", () => ({
  SignUp: (props: unknown) => { signUpProps(props); return null; },
}));
it("sends completed Clerk signup and existing sign-in flows through the chat workspace gate", () => {
  render(<Page />);
  expect(signUpProps).toHaveBeenCalledWith(expect.objectContaining({
    fallbackRedirectUrl: "/onboarding?returnTo=%2Fopen-app",
    signInFallbackRedirectUrl: "/chat",
  }));
  expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/sign-in");
  expect(screen.getByText("Free for 30 days")).toBeInTheDocument();
});
