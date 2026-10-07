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
    render(<FreeTierNote />);
    expect(screen.getByText(/2 days left on your free trial/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /see plans/i })).toHaveAttribute("href", "/settings?panel=billing");
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

  it("rounds a part-day UP, so the last hours read as a day rather than zero", () => {
    // freeTierDaysLeft ceils and anything past trial_end already returned null,
    // so a "0 days"/"last day" branch is unreachable by construction — "1 day
    // left" IS the last day. A branch that can never render is worse than none.
    billing.mockReturnValue(account({ trial_end: Math.floor(Date.now() / 1000) + 3600 }));
    render(<FreeTierNote />);
    expect(screen.getByText(/1 day left on your free trial/)).toBeInTheDocument();
    expect(screen.queryByText(/last day/i)).not.toBeInTheDocument();
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
