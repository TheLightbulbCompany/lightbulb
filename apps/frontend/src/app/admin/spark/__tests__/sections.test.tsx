import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { SparkData } from "../../_lib/api";
import { Channels, Cohorts, Funnel, Headline, Money, People, Reliability, Setup, Sources, kn } from "../sections";

const DATA: SparkData = {
  generated_at: "2026-11-24T12:00:00Z",
  wad: { last7: 5, prev7: 3, weekly: [{ week: "2026-11-23", partial: true, doers: 2 }] },
  cohorts: [
    {
      week: "2026-11-23",
      partial: true,
      platform: "mac",
      installs: 4,
      settled: 3,
      activated_24h: { k: 2, n: 3 },
      activated_d7: { k: 0, n: 0 },
      w4: null,
    },
  ],
  success: {
    weekly: [{ week: "2026-11-23", partial: true, platform: "mac", success: { k: 9, n: 10 } }],
    by_version: [{ key: "1.0", success: { k: 1, n: 2 } }],
    by_provider: [{ key: "claude", success: { k: 3, n: 4 } }],
    failed_reasons: [{ key: "auth", count: 5 }],
    problem_kinds: [{ key: "crash", count: 1 }],
  },
  brain_unavailable_installs_24h: 2,
  funnel: [{ step: "first launch", k: 4, n: 4 }],
  truncated: false,
  downloads: [{ key: "hero", count: 7 }],
  channels: [{ channel: "referral", activated_24h: { k: 1, n: 2 } }],
  people: [
    { installed: "2026-11-22", platform: "mac", version: "1.0", provider: "claude", channel: "referral", furthest: "first task", last_task: null, email: "a@b.co" },
  ],
  money: {
    price_usd: 20,
    subs: {
      paying: 2,
      mrr_usd: 40,
      unknown: 1,
      trial_to_paid: [{ month: "2026-10", k: 1, n: 4 }],
      cancellations: [{ month: "2026-11", count: 2, reasons: [{ key: "payment_failed", count: 2 }], feedback: [{ key: "too_expensive", count: 1 }] }],
    },
    voice: {
      rate_usd_per_min: 0.05,
      months: [{ month: "2026-11", minutes: 110, users: 2, p50: 55, p90: 100, cost_usd: 5.5 }],
      top: [{ email: "top@x.co", status: "active", minutes: 100, cost_usd: 5, margin_usd: 15 }],
    },
  },
  time_to_first_task: [
    { bucket: "lt5m", k: 1, n: 4 },
    { bucket: "never", k: 3, n: 4 },
  ],
  heard_from: [{ choice: "youtube", installs: 2, activated_24h: { k: 1, n: 2 } }],
  setup: {
    steps: [{ step: "brain", shown: 10, completed: { k: 6, n: 10 }, skipped: { k: 1, n: 10 } }],
    signin_failures: [{ key: "timeout", count: 3 }],
    permissions: [{ permission: "microphone", prompted: 8, denied: { k: 2, n: 8 } }],
    unfinished: { k: 3, n: 12 },
  },
};

const EMPTY: SparkData = {
  ...DATA,
  money: { price_usd: 20, subs: null, voice: null },
  heard_from: null,
  setup: null,
  time_to_first_task: [],
};

describe("spark sections", () => {
  it("renders the money, setup and sources figures", () => {
    render(
      <>
        <Money data={DATA} />
        <Setup data={DATA} />
        <Sources data={DATA} />
      </>,
    );
    expect(screen.getByText("MRR at list price")).toBeTruthy();
    expect(screen.getByText("$40")).toBeTruthy();
    expect(screen.getByText("1 / 4 (25%)")).toBeTruthy();
    expect(screen.getByText("top@x.co")).toBeTruthy();
    expect(screen.getByText("$15.00")).toBeTruthy();
    expect(screen.getAllByText(/estimate/).length).toBeGreaterThan(0);
    expect(screen.getByText("3 / 4 (75%)")).toBeTruthy();
    expect(screen.getByText("6 / 10 (60%)")).toBeTruthy();
    expect(screen.getByText(/timeout 3/)).toBeTruthy();
    expect(screen.getByText(/3 \/ 12 \(25%\)/)).toBeTruthy();
    expect(screen.getByText("youtube")).toBeTruthy();
  });

  it("shows no data yet, never zeros, when events were never seen", () => {
    render(
      <>
        <Money data={EMPTY} />
        <Setup data={EMPTY} />
        <Sources data={EMPTY} />
      </>,
    );
    expect(screen.getAllByText("no data yet")).toHaveLength(4);
    expect(screen.queryByText("Paying users")).toBeNull();
  });

  it("renders k / n (p%) and a dash without a denominator", () => {
    expect(kn({ k: 1, n: 2 })).toBe("1 / 2 (50%)");
    expect(kn({ k: 0, n: 0 })).toBe("—");
    expect(kn(null)).toBe("—");
  });

  it("renders every section, marks partial weeks and immature cohorts", () => {
    render(
      <>
        <Headline data={DATA} />
        <Funnel data={DATA} />
        <Cohorts data={DATA} />
        <Channels data={DATA} />
        <People data={DATA} />
        <Reliability data={DATA} />
      </>,
    );
    expect(screen.getAllByText("2026-11-23 (partial)").length).toBeGreaterThan(0);
    expect(screen.getByText("too early")).toBeTruthy();
    expect(screen.getByText("2 / 3 (67%)")).toBeTruthy();
    expect(screen.getByText("a@b.co")).toBeTruthy();
    expect(screen.getByText(/Channel attribution for non-referred users is not yet measurable/)).toBeTruthy();
  });
});
