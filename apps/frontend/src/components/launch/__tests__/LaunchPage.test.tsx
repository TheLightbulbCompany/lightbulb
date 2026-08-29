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
vi.mock("../MyLaunchCard", () => ({ MyLaunchCard: () => <div data-testid="me" /> }));
vi.mock("@/components/landing/Navbar", () => ({ Navbar: () => <nav /> }));
vi.mock("@/components/landing/Footer", () => ({ Footer: () => <footer /> }));

import LaunchPage from "../LaunchPage";

const base = { drop_at: "2026-09-18T16:00:00+00:00", competition_start_at: "2026-09-21T16:00:00+00:00", competition_end_at: "2026-10-21T16:00:00+00:00", pot_cents: 103000, signups: 40, competing: 3, on_board: 1, comp_slots: 220, pot_per_entrant_cents: 1000 };

describe("LaunchPage phases", () => {
  it("waitlist: sign-up CTA + sign-in link, shows the pot inside .ld-page", async () => {
    info.mockResolvedValue({ ...base, phase: "waitlist" });
    const { container } = render(<LaunchPage />);
    expect(await screen.findByRole("link", { name: /^sign up/i })).toHaveAttribute("href", "/sign-up");
    expect(screen.getByRole("link", { name: /sign in/i })).toHaveAttribute("href", "/sign-in");
    expect(screen.getByText("$1,030")).toBeInTheDocument();
    expect(container.querySelector("main.ld-page")).not.toBeNull();
    expect(screen.queryByTestId("me")).not.toBeInTheDocument();
  });
  it("waitlist: rules render comp_slots and the per-entrant pot dynamically", async () => {
    info.mockResolvedValue({ ...base, phase: "waitlist" });
    render(<LaunchPage />);
    expect(await screen.findByText(/the first 220 on the waitlist/)).toBeInTheDocument();
    expect(screen.getByText(/Every entrant puts \$10 into the pot/)).toBeInTheDocument();
  });
  it("activation: sign-up CTA, no sign-in link, no form", async () => {
    info.mockResolvedValue({ ...base, phase: "activation" });
    render(<LaunchPage />);
    expect(await screen.findByRole("link", { name: /^sign up/i })).toHaveAttribute("href", "/sign-up");
    expect(screen.queryByRole("link", { name: /sign in/i })).not.toBeInTheDocument();
  });
  it("live: leaderboard rows and the on-board/competing header", async () => {
    info.mockResolvedValue({ ...base, phase: "live" });
    board.mockResolvedValue({ competing: 2, on_board: 1, rows: [{ rank: 1, display_name: "Ada", delta_cents: 25000, pct: 0.5, currency: "usd" }] });
    render(<LaunchPage />);
    expect(await screen.findByText("Ada")).toBeInTheDocument();
    expect(screen.getByText("$250")).toBeInTheDocument();
    expect(screen.getByText("+50%")).toBeInTheDocument();
    expect(screen.getByText(/1 on the board · 2 competing/)).toBeInTheDocument();
  });
  it("ended: the board still renders, the sign-up CTA is gone", async () => {
    info.mockResolvedValue({ ...base, phase: "ended" });
    board.mockResolvedValue({ competing: 2, on_board: 1, rows: [{ rank: 1, display_name: "Ada", delta_cents: 25000, pct: 0.5, currency: "usd" }] });
    render(<LaunchPage />);
    expect(await screen.findByText("Ada")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /^sign up/i })).not.toBeInTheDocument();
  });
  it("feature off: quiet message", async () => {
    info.mockResolvedValue(null);
    render(<LaunchPage />);
    expect(await screen.findByText(/not open yet/i)).toBeInTheDocument();
  });
});
