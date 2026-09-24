import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const billing = vi.fn<() => unknown>();
vi.mock("@/hooks/useBilling", () => ({ useBilling: () => billing() }));
vi.mock("next/link", () => ({
  default: ({ href, children }: { href: string; children: React.ReactNode }) => <a href={href}>{children}</a>,
}));

import { FreeTierNote } from "../FreeTierNote";

const DAY = 86_400;
const account = (over: Record<string, unknown> = {}) => ({
  account: { subscription_status: "trial_local", trial_end: Math.floor(Date.now() / 1000) + 2 * DAY, ...over },
});

describe("FreeTierNote", () => {
  beforeEach(() => billing.mockReturnValue(account()));

  it("counts down the free-tier window, which nothing else in the product does", () => {
    // TrialBanner looks like the countdown but only matches LEGACY Stripe
    // `trialing` rows, so a trial_local owner previously met their deadline as
    // a paywall mid-conversation with no prior warning.
    billing.mockReturnValue(account({ trial_end: Math.floor(Date.now() / 1000) + 10 * DAY }));
    const { container } = render(<FreeTierNote />);
    expect(screen.getByText(/10 days left on your free trial/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /see plans/i })).toHaveAttribute("href", "/settings?panel=billing");
    expect(container.querySelector(".free-tier-note--urgent")).toBeNull();
  });

  it("switches to the warning state with a Choose a plan link at 3 days or fewer", () => {
    const { container } = render(<FreeTierNote />);
    expect(screen.getByText(/2 days left on your free trial/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /choose a plan/i })).toHaveAttribute("href", "/settings?panel=billing");
    expect(container.querySelector(".free-tier-note--urgent")).not.toBeNull();
  });

  it("clamps a fresh 30-day signup to 30 days, not 31, under a slow client clock", () => {
    // trial_end is signup + 30 days exactly, but the client clock reads 5 min
    // slow, so the raw ceil is 31. The clamp must bring it back to the promise.
    vi.useFakeTimers();
    try {
      const base = Date.UTC(2026, 0, 1, 12, 0, 0);
      vi.setSystemTime(base);
      const trialEnd = Math.floor(base / 1000) + 30 * DAY + 5 * 60;
      billing.mockReturnValue(account({ trial_end: trialEnd }));
      render(<FreeTierNote />);
      expect(screen.getByText(/30 days left on your free trial/)).toBeInTheDocument();
      expect(screen.queryByText(/31 days/)).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("says the last day is the last day", () => {
    // freeTierDaysLeft ceils and anything past trial_end already returned null,
    // so a remaining part-day is 1 — the last day — never 0.
    billing.mockReturnValue(account({ trial_end: Math.floor(Date.now() / 1000) + 3600 }));
    const { container } = render(<FreeTierNote />);
    expect(screen.getByText(/last day of your free trial/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /choose a plan/i })).toBeInTheDocument();
    expect(container.querySelector(".free-tier-note--urgent")).not.toBeNull();
  });

  it("renders nothing for a paid owner", () => {
    billing.mockReturnValue(account({ subscription_status: "active" }));
    const { container } = render(<FreeTierNote />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing once the window has elapsed", () => {
    // The blocked-state surfaces say it far better than one footer line could,
    // and the owner is about to hit them anyway.
    billing.mockReturnValue(account({ trial_end: Math.floor(Date.now() / 1000) - DAY }));
    const { container } = render(<FreeTierNote />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing for a legacy row with no trial_end", () => {
    // The backend reads a missing stamp as NOT expired, so inventing a
    // countdown for one would be a lie in the scary direction.
    billing.mockReturnValue(account({ trial_end: null }));
    const { container } = render(<FreeTierNote />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing before billing has loaded", () => {
    billing.mockReturnValue({ account: undefined });
    const { container } = render(<FreeTierNote />);
    expect(container).toBeEmptyDOMElement();
  });
});
