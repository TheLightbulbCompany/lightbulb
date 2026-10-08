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

it("sends completed Clerk signup and existing sign-in flows through the chat workspace gate", () => {
  render(<Page />);
  expect(signUpProps).toHaveBeenCalledWith(expect.objectContaining({
    fallbackRedirectUrl: "/chat", signInFallbackRedirectUrl: "/chat",
  }));
  expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/sign-in");
  expect(screen.getByText("Free for 30 days")).toBeInTheDocument();
});

it("offers the join option under the sign-up form, above the sign-in link", () => {
  render(<Page />);
  const form = screen.getByTestId("clerk-sign-up");
  const join = screen.getByLabelText("Have a join link?");
  const signIn = screen.getByRole("link", { name: "Sign in" });
  expect(screen.getByText(/If you got an invitation email, open it on this device/)).toBeInTheDocument();
  expect(form.compareDocumentPosition(join) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(join.compareDocumentPosition(signIn) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

it("routes a pasted join link to the join page", () => {
  render(<Page />);
  fireEvent.change(screen.getByLabelText("Have a join link?"), { target: { value: "https://thelightbulb.company/join/org_x#tok" } });
  fireEvent.click(screen.getByRole("button", { name: "Join" }));
  expect(push).toHaveBeenCalledWith("/join/org_x#tok");
});

it("refuses junk", () => {
  render(<Page />);
  fireEvent.change(screen.getByLabelText("Have a join link?"), { target: { value: "hello" } });
  fireEvent.click(screen.getByRole("button", { name: "Join" }));
  expect(screen.getByRole("alert")).toHaveTextContent("That doesn't look like a Lightbulb join link.");
  expect(push).not.toHaveBeenCalled();
});
