import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { HeroFunnel } from "../HeroFunnel";
import { LANDING_BRIEF_STORAGE_KEY } from "@/hooks/useLandingBriefReplay";
import { SIGNUP_INTENT_COOKIE } from "@/lib/signupIntent";
import { getTheme, setTheme } from "../HeroLamp/lampStore";

const { pushMock } = vi.hoisted(() => ({ pushMock: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock }),
}));

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

// The hero reads the Launch Competition phase; `null` = no competition, the
// normal hero. One test flips it to "waitlist" to prove the pill swaps.
const { launchPhase } = vi.hoisted(() => ({ launchPhase: { value: null as string | null } }));
vi.mock("@/components/launch/useLaunchPhase", () => ({
  useLaunchPhase: () => launchPhase.value,
}));
vi.mock("@/components/launch/WaitlistForm", () => ({
  WaitlistForm: () => <form aria-label="Join the waitlist" />,
}));

function readStash(): { path: string; input: string; detail: string } | null {
  const raw = window.sessionStorage.getItem(LANDING_BRIEF_STORAGE_KEY);
  return raw ? JSON.parse(raw) : null;
}

describe("HeroFunnel", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    pushMock.mockClear();
    launchPhase.value = null;
  });

  it("swaps the idea pill for the waitlist form while the waitlist is open", () => {
    launchPhase.value = "waitlist";
    render(<HeroFunnel />);
    expect(screen.getByRole("form", { name: /join the waitlist/i })).toBeInTheDocument();
    expect(screen.queryByLabelText("Your idea")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Start/ })).not.toBeInTheDocument();
    expect(screen.getByText(/Join the waitlist\./)).toBeInTheDocument();
    // The headline is the product's, not the competition's — it stays.
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      "Type an idea. Watch it become a company.",
    );
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
        /Your company gets a name, a working email address, and its first/,
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

describe("HeroFunnel lights the room", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    setTheme("dark");
  });

  it("the first character typed turns the light on", () => {
    render(<HeroFunnel />);
    const input = screen.getByRole("textbox", { name: /your idea/i });
    expect(getTheme()).toBe("dark");
    fireEvent.change(input, { target: { value: "A" } });
    expect(getTheme()).toBe("light");
  });

  it("typing never turns the light back off", () => {
    render(<HeroFunnel />);
    const input = screen.getByRole("textbox", { name: /your idea/i });
    fireEvent.change(input, { target: { value: "A" } });
    fireEvent.change(input, { target: { value: "" } });
    fireEvent.change(input, { target: { value: "B" } });
    expect(getTheme()).toBe("light");
  });
});
