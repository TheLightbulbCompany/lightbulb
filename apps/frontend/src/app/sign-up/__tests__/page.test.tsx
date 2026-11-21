// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import Page from "../[[...sign-up]]/page";

const signUpProps = vi.hoisted(() => vi.fn());
const push = vi.hoisted(() => vi.fn());
vi.mock("@clerk/nextjs", () => ({
  SignUp: (props: unknown) => { signUpProps(props); return <div data-testid="clerk-sign-up" />; },
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }), useSearchParams: () => new URLSearchParams() }));
beforeEach(() => vi.clearAllMocks());

const openJoinDoor = () => fireEvent.click(screen.getByRole("tab", { name: /I was invited/ }));

it("speaks to an owner setting up a phone assistant, with no free-trial promise", () => {
  render(<Page />);
  expect(screen.getByRole("heading", { level: 1, hidden: true })).toHaveTextContent("Your phone, answered and booked.");
  for (const line of ["Answers day and night", "Set up in five steps", "You stay in charge", "Pay when you go live"]) {
    expect(screen.getByText(line)).toBeInTheDocument();
  }
  expect(screen.queryByText(/Free for 30 days|Your team in the same rooms|Agents that ask first/)).toBeNull();
});

it("opens on the create door: the Clerk form, landing on the voice home", () => {
  render(<Page />);
  expect(screen.getByRole("tab", { name: /Set up my shop/ })).toHaveAttribute("aria-selected", "true");
  expect(signUpProps).toHaveBeenCalledWith(expect.objectContaining({
    fallbackRedirectUrl: "/voice", signInFallbackRedirectUrl: "/voice",
  }));
  expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/sign-in");
});

it("the join door takes a pasted join link to the join page", () => {
  render(<Page />);
  openJoinDoor();
  expect(screen.queryByTestId("clerk-sign-up")).toBeNull();
  fireEvent.change(screen.getByLabelText("Join link"), { target: { value: "https://thelightbulb.company/join/org_x#tok" } });
  fireEvent.click(screen.getByRole("button", { name: "Join" }));
  expect(push).toHaveBeenCalledWith("/join/org_x#tok");
});

it("refuses junk", () => {
  render(<Page />);
  openJoinDoor();
  fireEvent.change(screen.getByLabelText("Join link"), { target: { value: "hello" } });
  fireEvent.click(screen.getByRole("button", { name: "Join" }));
  expect(screen.getByRole("alert")).toHaveTextContent("That doesn't look like a Lightbulb join link.");
  expect(push).not.toHaveBeenCalled();
});
