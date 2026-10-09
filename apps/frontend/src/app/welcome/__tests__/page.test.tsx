// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const clerk = vi.hoisted(() => ({
  isLoaded: true,
  user: null as null | { firstName: string | null; username?: string | null; update: ReturnType<typeof vi.fn> },
  replace: vi.fn(),
}));
// One router object, as in Next: a new one per render would re-run the page's effect.
const router = vi.hoisted(() => ({ replace: (...args: unknown[]) => clerk.replace(...args) }));
vi.mock("next/navigation", () => ({ useRouter: () => router }));
vi.mock("@clerk/nextjs", () => ({ useUser: () => ({ isLoaded: clerk.isLoaded, user: clerk.user }) }));

import NamePage from "../name/page";

const at = (search: string) => window.history.replaceState(null, "", `/welcome/name${search}`);

beforeEach(() => {
  clerk.replace.mockReset();
  clerk.isLoaded = true;
  clerk.user = { firstName: null, update: vi.fn() };
  // What Clerk does: the saved name lands on the same `user` the hook hands out.
  clerk.user.update.mockImplementation(async (fields: { firstName: string }) => {
    clerk.user = { ...clerk.user!, ...fields };
  });
  at("?returnTo=%2Fjoin%2Forg_1");
});

describe("/welcome/name", () => {
  it("saves the name to Clerk and goes back to where the person was headed", async () => {
    const user = userEvent.setup();
    const update = clerk.user!.update;
    const { rerender } = render(<NamePage />);

    expect(screen.getByRole("heading", { name: "What should people call you?" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue" })).toBeDisabled();
    await user.type(screen.getByLabelText("First name"), " Sam ");
    await user.type(screen.getByLabelText("Last name (optional)"), "Rivera");
    await user.click(screen.getByRole("button", { name: "Continue" }));

    await waitFor(() => expect(update).toHaveBeenCalledWith({ firstName: "Sam", lastName: "Rivera" }));
    rerender(<NamePage />); // Clerk re-renders its hooks' users with the saved name
    await waitFor(() => expect(clerk.replace).toHaveBeenCalledWith("/join/org_1"));
    expect(clerk.replace).toHaveBeenCalledTimes(1);
  });

  it.each(["https://evil.example/chat", "//evil.example", "/\\evil.example", ""])("refuses an off-site returnTo: %s", (target) => {
    clerk.user = { firstName: "Ada", update: vi.fn() };
    at(target ? `?returnTo=${encodeURIComponent(target)}` : "");
    render(<NamePage />);
    expect(clerk.replace).toHaveBeenCalledWith("/chat");
  });

  it.each([{ firstName: "Ada" }, { firstName: null, username: "ada" }])("sends a person who already has a name straight on: %j", (named) => {
    clerk.user = { ...named, update: vi.fn() };
    render(<NamePage />);
    expect(clerk.replace).toHaveBeenCalledWith("/join/org_1");
    expect(screen.queryByLabelText("First name")).not.toBeInTheDocument();
  });

  it("stays put and says so when Clerk refuses the save", async () => {
    clerk.user!.update.mockRejectedValue(new Error("nope"));
    const user = userEvent.setup();
    render(<NamePage />);
    await user.type(screen.getByLabelText("First name"), "Sam");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("couldn't save your name");
    expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled();
    expect(clerk.replace).not.toHaveBeenCalled();
  });

  it("sends a signed-out visitor to sign in", () => {
    clerk.user = null;
    render(<NamePage />);
    expect(clerk.replace).toHaveBeenCalledWith("/sign-in");
  });
});
