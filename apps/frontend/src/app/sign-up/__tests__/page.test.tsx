// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import Page from "../[[...sign-up]]/page";
import { authFallbackUrl } from "@/lib/domain";

const signUpProps = vi.hoisted(() => vi.fn());
vi.mock("@clerk/nextjs", () => ({
  SignUp: (props: unknown) => { signUpProps(props); return <div data-testid="clerk-sign-up" />; },
}));
beforeEach(() => vi.clearAllMocks());

it("speaks about Spark, not the retired phone product", () => {
  render(<Page />);
  expect(screen.getByRole("heading", { level: 1, hidden: true })).toHaveTextContent("One-click voice interface for your Mac.");
  expect(screen.queryByText(/phone|booked|go live|business/i)).toBeNull();
});

it("sign up has one door: the Clerk form, landing on Spark's page, and no join door", () => {
  render(<Page />);
  expect(signUpProps).toHaveBeenCalledWith(expect.objectContaining({
    fallbackRedirectUrl: "/os", signInFallbackRedirectUrl: "/os",
  }));
  expect(screen.queryByRole("tab")).toBeNull();
  expect(screen.queryByLabelText("Join link")).toBeNull();
  expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/sign-in");
});

it("lands on the admin's root on an admin host", () => {
  expect(authFallbackUrl("admin.thelightbulb.company")).toBe("/");
  expect(authFallbackUrl("thelightbulb.company")).toBe("/os");
  expect(authFallbackUrl(undefined)).toBe("/os");
});
