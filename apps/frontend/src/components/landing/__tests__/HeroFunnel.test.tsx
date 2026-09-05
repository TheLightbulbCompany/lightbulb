import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { HeroFunnel } from "../HeroFunnel";
import { LANDING_BRIEF_STORAGE_KEY } from "@/hooks/useLandingBriefReplay";
import { SIGNUP_INTENT_COOKIE } from "@/lib/signupIntent";
import { getTheme, setTheme } from "../HeroLamp/lampStore";

const { pushMock, authState } = vi.hoisted(() => ({
  pushMock: vi.fn(),
  authState: { isSignedIn: false },
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock }),
}));

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

vi.mock("@/components/launch/LaunchHeroPill", () => ({ LaunchHeroPill: () => null }));

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ isSignedIn: authState.isSignedIn, getToken: async () => null }),
}));

// BACKEND_URL feeds useLaunchInfo's real fetch (stubbed per-test below).
vi.mock("@/lib/api", () => ({
  BACKEND_URL: "http://api.test/api/v1",
}));

function readStash(): { path: string; input: string; detail: string } | null {
  const raw = window.sessionStorage.getItem(LANDING_BRIEF_STORAGE_KEY);
  return raw ? JSON.parse(raw) : null;
}

describe("HeroFunnel", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    pushMock.mockClear();
  });

  it("renders the you-deserve-a-team fold: eyebrow, headline, one microcopy line", () => {
    render(<HeroFunnel />);
    expect(
      screen.getByText("The Integrated Management Environment"),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      "If you have an idea, you deserve a team.",
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

describe("HeroFunnel launch framing", () => {
  const launchInfo = {
    phase: "open",
    sheet_closes_at: "2026-09-30T16:00:00+00:00",
    competition_start_at: "2026-10-01T16:00:00+00:00",
    competition_end_at: "2026-12-01T16:00:00+00:00",
    signups: 1234,
    comp_slots: 200,
    referral_jump: 10,
  };

  beforeEach(() => {
    window.sessionStorage.clear();
    authState.isSignedIn = true;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => launchInfo }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    authState.isSignedIn = false;
  });

  it("frames the CTA as joining the launch while the sheet is open", async () => {
    render(<HeroFunnel />);
    expect(await screen.findByRole("button", { name: "Join the launch" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toBeInTheDocument();
  });

  it("keeps the plain Start CTA once the sheet is closed", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ ...launchInfo, phase: "ended" }) }),
    );
    render(<HeroFunnel />);
    // The fetch resolves in an effect; the framing never flips for a dead phase.
    expect(await screen.findByRole("button", { name: /start/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Join the launch" })).not.toBeInTheDocument();
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
