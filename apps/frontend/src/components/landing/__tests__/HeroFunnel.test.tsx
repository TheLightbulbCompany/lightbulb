import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { HeroFunnel } from "../HeroFunnel";
import { LANDING_BRIEF_STORAGE_KEY } from "@/hooks/useLandingBriefReplay";
import { SIGNUP_INTENT_COOKIE } from "@/lib/signupIntent";
import { getTheme, setTheme } from "../HeroLamp/lampStore";

const { pushMock, apiGet, authState } = vi.hoisted(() => ({
  pushMock: vi.fn(),
  apiGet: vi.fn(),
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

// BACKEND_URL feeds useLaunchInfo's real fetch (stubbed per-test below);
// useApi feeds the held-owner /launch/me read.
vi.mock("@/lib/api", () => ({
  BACKEND_URL: "http://api.test/api/v1",
  useApi: () => ({ get: apiGet, post: vi.fn(), put: vi.fn(), patch: vi.fn(), del: vi.fn() }),
}));

vi.mock("../LaunchSpotCard", () => ({
  LaunchSpotCard: () => <div data-testid="launch-spot-card" />,
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

describe("HeroFunnel during the launch waitlist phase", () => {
  const launchInfo = {
    phase: "waitlist",
    drop_at: "2026-09-18T16:00:00+00:00",
    competition_start_at: "2026-09-25T16:00:00+00:00",
    competition_end_at: "2026-10-25T16:00:00+00:00",
    signups: 1234,
    competing: 0,
    on_board: 0,
    pot_cents: 103000,
    comp_slots: 220,
    pot_per_entrant_cents: 1000,
    referral_jump: 10,
  };
  const heldMe = {
    phase: "waitlist",
    waitlist: null,
    activated: false,
    activated_at: null,
    entry: null,
    joined: false,
    has_key: false,
    can_join: false,
    reason: null,
    idea: null,
    idea_full: null,
  };

  beforeEach(() => {
    window.sessionStorage.clear();
    apiGet.mockReset();
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

  it("defaults to the funnel, then swaps in the spot card once /launch/me confirms held", async () => {
    apiGet.mockResolvedValue(heldMe);
    render(<HeroFunnel />);
    // Default render is the ordinary hero — no layout shift before the reads resolve.
    expect(screen.getByRole("heading", { level: 1 })).toBeInTheDocument();
    expect(await screen.findByTestId("launch-spot-card")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 1 })).not.toBeInTheDocument();
    expect(apiGet).toHaveBeenCalledWith("/launch/me");
  });

  it("keeps the funnel for an activated (existing) owner — only the CTA reframes", async () => {
    apiGet.mockResolvedValue({ ...heldMe, activated: true });
    render(<HeroFunnel />);
    expect(await screen.findByRole("button", { name: "Join the launch" })).toBeInTheDocument();
    expect(screen.queryByTestId("launch-spot-card")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toBeInTheDocument();
  });

  it("keeps the funnel for a competition entrant (joined)", async () => {
    apiGet.mockResolvedValue({ ...heldMe, joined: true });
    render(<HeroFunnel />);
    expect(await screen.findByRole("button", { name: "Join the launch" })).toBeInTheDocument();
    expect(screen.queryByTestId("launch-spot-card")).not.toBeInTheDocument();
  });

  it("signed out: never calls /launch/me, and the CTA reads 'Join the launch'", async () => {
    authState.isSignedIn = false;
    render(<HeroFunnel />);
    expect(await screen.findByRole("button", { name: "Join the launch" })).toBeInTheDocument();
    expect(apiGet).not.toHaveBeenCalled();
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
