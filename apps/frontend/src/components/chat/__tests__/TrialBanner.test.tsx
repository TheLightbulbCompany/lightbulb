import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SWRConfig } from "swr";
import React from "react";

// TrialBanner reads /billing/account via useSWR; feed it synchronously through
// SWRConfig `fallback` so the banner renders on the first pass without awaiting.
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: vi.fn().mockResolvedValue(null) }) }));
vi.mock("next/link", () => ({
  default: ({ href, children }: { href: string; children: React.ReactNode }) => (
    <a href={href}>{children}</a>
  ),
}));

import { TrialBanner } from "../TrialBanner";

function renderWith(account: {
  subscription_status?: string;
  trial_end?: number;
  workspace_price_usd?: number;
}) {
  return render(
    <SWRConfig value={{ fallback: { "/billing/account": account }, provider: () => new Map() }}>
      <TrialBanner />
    </SWRConfig>,
  );
}

describe("TrialBanner", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("renders the charge date in UTC, not the host timezone", () => {
    // now = 2026-06-14T20:00Z, trial_end = 2026-06-15T04:00Z (8h -> 1 day left,
    // inside the reminder window). In any zone west of UTC the local date is
    // Jun 14 while the UTC date is Jun 15, so a UTC-pinned render must show 15.
    //
    // Node caches its timezone at process start, so `process.env.TZ` at runtime
    // is a no-op (see useCompanyRollup.test.ts) — on a UTC/CI host this asserts
    // the UTC date trivially; prove it catches the bug with
    //   TZ=America/Los_Angeles npx vitest run TrialBanner
    vi.setSystemTime(new Date("2026-06-14T20:00:00Z"));
    const trialEnd = Math.floor(Date.parse("2026-06-15T04:00:00Z") / 1000);
    renderWith({ subscription_status: "trialing", trial_end: trialEnd });

    // Date is wrapped in parens in the banner copy; assert the UTC day (15) is
    // there and the host-local day (14) is not, independent of locale ordering.
    expect(screen.getByText(/\(.*\b15\b.*\)/)).toBeInTheDocument();
    expect(screen.queryByText(/\(.*\b14\b.*\)/)).not.toBeInTheDocument();
  });

  it("renders nothing for a non-legacy status (dead once the last trialing row converts)", () => {
    vi.setSystemTime(new Date("2026-06-14T20:00:00Z"));
    const trialEnd = Math.floor(Date.parse("2026-06-15T04:00:00Z") / 1000);
    const { container } = renderWith({ subscription_status: "trial_local", trial_end: trialEnd });
    expect(container).toBeEmptyDOMElement();
  });

  it("uses the server-owned workspace price when it is available", () => {
    vi.setSystemTime(new Date("2026-06-14T20:00:00Z"));
    const trialEnd = Math.floor(Date.parse("2026-06-15T04:00:00Z") / 1000);
    renderWith({
      subscription_status: "trialing",
      trial_end: trialEnd,
      workspace_price_usd: 75,
    });

    expect(screen.getByText(/then \$75\/mo\./)).toBeInTheDocument();
  });
});

it("the clock snapshot is stable within a minute, so React never sees a changed store on every render", async () => {
  const { getNow } = await import("../TrialBanner");
  const a = getNow();
  const b = getNow();
  expect(a).toBe(b);
  expect(a % 60_000).toBe(0);
});
