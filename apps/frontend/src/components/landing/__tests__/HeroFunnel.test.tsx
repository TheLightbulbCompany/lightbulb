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

  it("renders the outcome fold: headline, two doors, deliverables, price", () => {
    render(<HeroFunnel />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      "An AI team that runs the business, a CEO that manages it.",
    );
    // The eyebrow pun and the feeling-not-promise headline are gone.
    expect(screen.queryByText(/Integrated Management/)).not.toBeInTheDocument();
    expect(screen.queryByText(/deserve a team/)).not.toBeInTheDocument();
    // Both doors, grow pre-selected.
    expect(screen.getByRole("button", { name: "Grow my business" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "Start something new" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    expect(screen.getByLabelText("Your company's website")).toBeInTheDocument();
    // Deliverables and the three ways to pay sit above the fold.
    expect(
      screen.getByText(/Five minutes in, your CEO has researched the market/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/ChatGPT login, your own API key, or Claude credits/),
    ).toBeInTheDocument();
  });

  it("stashes {path, input, detail} to sessionStorage on every change", () => {
    render(<HeroFunnel />);
    fireEvent.change(screen.getByLabelText("Your company's website"), {
      target: { value: "acme.com" },
    });
    expect(readStash()).toEqual({
      path: "grow",
      input: "acme.com",
      detail: "",
    });
  });

  it("switches to the new path through its door and stashes the new path", () => {
    render(<HeroFunnel />);
    fireEvent.change(screen.getByLabelText("Your company's website"), {
      target: { value: "A ceramics studio" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Start something new" }));
    expect(readStash()).toEqual({ path: "new", input: "A ceramics studio", detail: "" });
    // The door reads pressed and the input relabels.
    expect(screen.getByRole("button", { name: "Start something new" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByLabelText("Your idea")).toBeInTheDocument();
  });

  it("routes to /sign-up on submit without writing the team-intent cookie", () => {
    render(<HeroFunnel />);
    fireEvent.change(screen.getByLabelText("Your company's website"), {
      target: { value: "acme.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /start$/i }));
    expect(pushMock).toHaveBeenCalledWith("/sign-up");
    expect(document.cookie).not.toContain(SIGNUP_INTENT_COOKIE);
    expect(readStash()?.path).toBe("grow");
  });

  it("does not submit under the 3-character minimum", () => {
    render(<HeroFunnel />);
    fireEvent.change(screen.getByLabelText("Your company's website"), {
      target: { value: "ab" },
    });
    const start = screen.getByRole("button", { name: /start$/i });
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
    expect(await screen.findByRole("button", { name: /start$/i })).toBeInTheDocument();
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
    const input = screen.getByRole("textbox", { name: /your company's website/i });
    expect(getTheme()).toBe("dark");
    fireEvent.change(input, { target: { value: "A" } });
    expect(getTheme()).toBe("light");
  });

  it("typing never turns the light back off", () => {
    render(<HeroFunnel />);
    const input = screen.getByRole("textbox", { name: /your company's website/i });
    fireEvent.change(input, { target: { value: "A" } });
    fireEvent.change(input, { target: { value: "" } });
    fireEvent.change(input, { target: { value: "B" } });
    expect(getTheme()).toBe("light");
  });
});
