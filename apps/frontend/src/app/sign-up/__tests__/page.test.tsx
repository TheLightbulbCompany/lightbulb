// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import Page from "../[[...sign-up]]/page";

const signUpProps = vi.hoisted(() => vi.fn());
vi.mock("@clerk/nextjs", () => ({
  SignUp: (props: unknown) => { signUpProps(props); return <div data-testid="clerk-sign-up" />; },
}));
beforeEach(() => vi.clearAllMocks());

it("speaks to an owner setting up a phone assistant, with no free-trial promise", () => {
  render(<Page />);
  expect(screen.getByRole("heading", { level: 1, hidden: true })).toHaveTextContent("Your phone, answered and booked.");
  for (const line of ["Answers day and night", "Set up in five steps", "You stay in charge", "Pay when you go live"]) {
    expect(screen.getByText(line)).toBeInTheDocument();
  }
  expect(screen.queryByText(/Free for 30 days|Your team in the same rooms|Agents that ask first/)).toBeNull();
});

it("sign up has one door: the Clerk form, landing on the voice home, and no join door", () => {
  render(<Page />);
  expect(signUpProps).toHaveBeenCalledWith(expect.objectContaining({
    fallbackRedirectUrl: "/voice", signInFallbackRedirectUrl: "/voice",
  }));
  expect(screen.queryByRole("tab")).toBeNull();
  expect(screen.queryByLabelText("Join link")).toBeNull();
  expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/sign-in");
});
