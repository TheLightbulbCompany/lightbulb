import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { HeroFunnel } from "../HeroFunnel";
import { LANDING_BRIEF_STORAGE_KEY } from "@/hooks/useLandingBriefReplay";
import { SIGNUP_INTENT_COOKIE } from "@/lib/signupIntent";

const { pushMock } = vi.hoisted(() => ({ pushMock: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock }),
}));

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

function readStash(): { path: string; input: string; detail: string } | null {
  const raw = window.sessionStorage.getItem(LANDING_BRIEF_STORAGE_KEY);
  return raw ? JSON.parse(raw) : null;
}

describe("HeroFunnel", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    pushMock.mockClear();
  });

  it("renders the idea-to-company fold: eyebrow, headline, one microcopy line", () => {
    render(<HeroFunnel />);
    expect(
      screen.getByText("The Integrated Management Environment"),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      "Type an idea. Watch it become a company.",
    );
    expect(
      screen.getByText(
        /Your own persistent, isolated cloud container starts building/,
      ),
    ).toBeInTheDocument();
    // The old subheadline and trial line are gone from the fold.
    expect(screen.queryByText(/Tell your AI CEO/)).not.toBeInTheDocument();
    expect(screen.queryByText(/3-day free trial/)).not.toBeInTheDocument();
  });

  it("stashes {path, input, detail} to sessionStorage on every change", () => {
    render(<HeroFunnel />);
    fireEvent.change(screen.getByLabelText("Your idea"), {
      target: { value: "A ceramics studio" },
    });
    expect(readStash()).toEqual({
      path: "new",
      input: "A ceramics studio",
      detail: "",
    });
  });

  it("swaps to the grow path via the quiet line and stashes the new path", () => {
    render(<HeroFunnel />);
    fireEvent.change(screen.getByLabelText("Your idea"), {
      target: { value: "acme.com" },
    });
    fireEvent.click(
      screen.getByRole("button", {
        name: "Already have a business? Grow it instead",
      }),
    );
    expect(readStash()).toEqual({ path: "grow", input: "acme.com", detail: "" });
    // The swap line now offers the way back, and the input relabels.
    expect(
      screen.getByRole("button", {
        name: "Starting from scratch? Describe your idea instead",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText("Your company's website"),
    ).toBeInTheDocument();
  });

  it("routes to /sign-up on submit without writing the team-intent cookie", () => {
    render(<HeroFunnel />);
    fireEvent.change(screen.getByLabelText("Your idea"), {
      target: { value: "A ceramics studio that ships nationwide" },
    });
    fireEvent.click(screen.getByRole("button", { name: /start/i }));
    expect(pushMock).toHaveBeenCalledWith("/sign-up");
    expect(document.cookie).not.toContain(SIGNUP_INTENT_COOKIE);
    expect(readStash()?.path).toBe("new");
  });

  it("does not submit under the 3-character minimum", () => {
    render(<HeroFunnel />);
    fireEvent.change(screen.getByLabelText("Your idea"), {
      target: { value: "ab" },
    });
    const start = screen.getByRole("button", { name: /start/i });
    expect(start).toBeDisabled();
    fireEvent.click(start);
    expect(pushMock).not.toHaveBeenCalled();
  });
});
