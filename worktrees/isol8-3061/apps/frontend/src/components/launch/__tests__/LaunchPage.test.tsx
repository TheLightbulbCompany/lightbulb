import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const info = vi.fn();
const board = vi.fn();
vi.mock("@/lib/launch", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/launch")>()),
  fetchLaunchInfo: () => info(),
  fetchLeaderboard: () => board(),
}));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ isSignedIn: false, isLoaded: true }), UserButton: () => null }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(""), usePathname: () => "/launch" }));
vi.mock("../SheetCard", () => ({ SheetCard: () => <div data-testid="me" /> }));
vi.mock("@/components/landing/Navbar", () => ({ Navbar: () => <nav /> }));
vi.mock("@/components/landing/Footer", () => ({ Footer: () => <footer /> }));

import LaunchPage from "../LaunchPage";

const base = {
  sheet_closes_at: "2026-09-30T16:00:00+00:00",
  competition_start_at: "2026-10-01T16:00:00+00:00",
  competition_end_at: "2026-12-01T16:00:00+00:00",
  signups: 40,
  comp_slots: 200,
  referral_jump: 10,
};

describe("LaunchPage phases", () => {
  it("open: sign-up CTA + sign-in link inside .ld-page, no board, no signed-in card", async () => {
    info.mockResolvedValue({ ...base, phase: "open" });
    const { container } = render(<LaunchPage />);
    expect(await screen.findByRole("link", { name: /^sign up/i })).toHaveAttribute("href", "/sign-up");
    expect(screen.getByRole("link", { name: /sign in/i })).toHaveAttribute("href", "/sign-in");
    expect(container.querySelector("main.ld-page")).not.toBeNull();
    expect(screen.queryByTestId("me")).not.toBeInTheDocument();
    expect(container.querySelector("#board")).toBeNull();
  });

  it("open: rules render the payload's numbers and never mention a pot", async () => {
    info.mockResolvedValue({ ...base, phase: "open" });
    const { container } = render(<LaunchPage />);
    expect(await screen.findByText(/The first 200 on the sheet get their first month free\./)).toBeInTheDocument();
    expect(screen.getByText(/The sheet closes September 30 at 12:00 PM ET\./)).toBeInTheDocument();
    expect(screen.getByText(/Every referral who subscribes moves you up 10 spots — the first 20 count\./)).toBeInTheDocument();
    expect(screen.getByText(/5 subscribed referrals earn \$50 in credits; 20 earn \$250 total\./)).toBeInTheDocument();
    expect(screen.getByText(/The competition runs October 1 – December 1/)).toBeInTheDocument();
    expect(screen.getByText(/Judges pick the winner/)).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/\bpot\b/i); // "spots" is fine — the POT is what may never appear
  });

  it("open: the hero counts signups (never 'on the sheet') and counts down to the close", async () => {
    info.mockResolvedValue({ ...base, phase: "open" });
    render(<LaunchPage />);
    expect(await screen.findByText("signups")).toBeInTheDocument();
    expect(screen.getByText("40")).toBeInTheDocument();
    expect(screen.getByText("until the sheet closes")).toBeInTheDocument();
  });

  it("closed: counts down to the competition start, no sign-up CTA", async () => {
    info.mockResolvedValue({ ...base, phase: "closed" });
    render(<LaunchPage />);
    expect(await screen.findByText("until the competition starts")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /^sign up/i })).not.toBeInTheDocument();
  });

  it("competition: leaderboard rows and the on-board/competing header", async () => {
    info.mockResolvedValue({ ...base, phase: "competition" });
    board.mockResolvedValue({ competing: 2, on_board: 1, rows: [{ rank: 1, display_name: "Ada", delta_cents: 25000, pct: 0.5, currency: "usd" }] });
    render(<LaunchPage />);
    expect(await screen.findByText("Ada")).toBeInTheDocument();
    expect(screen.getByText("$250")).toBeInTheDocument();
    expect(screen.getByText("+50%")).toBeInTheDocument();
    expect(screen.getByText(/1 on the board · 2 competing/)).toBeInTheDocument();
  });

  it("ended: the board still renders, the countdown stat is gone", async () => {
    info.mockResolvedValue({ ...base, phase: "ended" });
    board.mockResolvedValue({ competing: 2, on_board: 1, rows: [] });
    render(<LaunchPage />);
    expect(await screen.findByText(/No public entrants yet/)).toBeInTheDocument();
    expect(screen.queryByText(/until the/)).not.toBeInTheDocument();
  });

  it("feature off: quiet message", async () => {
    info.mockResolvedValue(null);
    render(<LaunchPage />);
    expect(await screen.findByText(/not open yet/i)).toBeInTheDocument();
  });
});
