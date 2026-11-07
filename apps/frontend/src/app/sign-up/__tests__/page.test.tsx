// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import Page from "../[[...sign-up]]/page";

const signUpProps = vi.hoisted(() => vi.fn());
const push = vi.hoisted(() => vi.fn());
vi.mock("@clerk/nextjs", () => ({
  SignUp: (props: unknown) => { signUpProps(props); return <div data-testid="clerk-sign-up" />; },
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
beforeEach(() => vi.clearAllMocks());

const openJoinDoor = () => fireEvent.click(screen.getByRole("tab", { name: /Join your team/ }));

it("sells the team, not a solo tool", () => {
  render(<Page />);
  expect(screen.getByRole("heading", { level: 1, hidden: true })).toHaveTextContent("Bring your team.");
  for (const line of ["Your team in the same rooms", "Your own cloud", "Agents that ask first", "Free for 30 days"]) {
    expect(screen.getByText(line)).toBeInTheDocument();
  }
  expect(screen.queryByText(/No shared data/)).toBeNull();
});

it("opens on the create door: the Clerk form, landing on the voice home", () => {
  render(<Page />);
  expect(screen.getByRole("tab", { name: /Create a workspace/ })).toHaveAttribute("aria-selected", "true");
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
