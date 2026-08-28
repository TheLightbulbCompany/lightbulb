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
vi.mock("../WaitlistForm", () => ({ WaitlistForm: () => <form aria-label="Join the waitlist" /> }));
vi.mock("@/components/landing/Navbar", () => ({ Navbar: () => <nav /> }));
vi.mock("@/components/landing/Footer", () => ({ Footer: () => <footer /> }));

import LaunchPage from "../LaunchPage";

const base = { drop_at: "2026-09-18T16:00:00+00:00", competition_start_at: "2026-09-21T16:00:00+00:00", competition_end_at: "2026-10-21T16:00:00+00:00", pot_cents: 103000, entrants: 3, waitlist_count: 40, comp_slots: 220 };

describe("LaunchPage phases", () => {
  it("waitlist: embeds the form and shows the pot inside .ld-page", async () => {
    info.mockResolvedValue({ ...base, phase: "waitlist" });
    const { container } = render(<LaunchPage />);
    expect(await screen.findByRole("form", { name: /join the waitlist/i })).toBeInTheDocument();
    expect(screen.getByText("$1,030")).toBeInTheDocument();
    expect(container.querySelector("main.ld-page")).not.toBeNull();
    expect(screen.queryByTestId("me")).not.toBeInTheDocument();
  });
  it("activation: sign-up CTA, no form", async () => {
    info.mockResolvedValue({ ...base, phase: "activation" });
    render(<LaunchPage />);
    expect(await screen.findByRole("link", { name: /sign up/i })).toHaveAttribute("href", "/sign-up");
    expect(screen.queryByRole("form", { name: /join the waitlist/i })).not.toBeInTheDocument();
  });
  it("live: leaderboard rows", async () => {
    info.mockResolvedValue({ ...base, phase: "live" });
    board.mockResolvedValue({ entrants: 1, rows: [{ rank: 1, display_name: "Ada", delta_cents: 25000, pct: 0.5, currency: "usd" }] });
    render(<LaunchPage />);
    expect(await screen.findByText("Ada")).toBeInTheDocument();
    expect(screen.getByText("$250")).toBeInTheDocument();
    expect(screen.getByText("+50%")).toBeInTheDocument();
  });
  it("feature off: quiet message", async () => {
    info.mockResolvedValue(null);
    render(<LaunchPage />);
    expect(await screen.findByText(/not open yet/i)).toBeInTheDocument();
  });
});
