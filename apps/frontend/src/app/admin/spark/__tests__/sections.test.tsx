import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { SparkData } from "../../_lib/api";
import { Channels, Cohorts, Funnel, Headline, Money, People, Reliability, Setup, Signals, Sources, kn } from "../sections";

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
      trial_to_paid: [{ month: "2026-10", k: 1, n: 4, partial: true }],
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
  signals: {
    stopped: { k: 3, n: 12, how: [{ key: "pressed", count: 2 }, { key: "said", count: 1 }, { key: "other", count: 0 }] },
    automations: { runs: 10, ok: { k: 8, n: 10 }, with_brain: { k: 7, n: 10 }, people_30d: 3 },
    features: [
      { feature: "push to talk", people_30d: 4, people_7d: 3 },
      { feature: "full access", people_30d: 2, people_7d: null },
    ],
    crash: { k: 2, n: 7 },
    versions: [{ version: "1.0", people_7d: 4 }],
    updates: [{ to_version: "1.1", installs_30d: 3 }],
  },
};

const EMPTY: SparkData = {
  ...DATA,
  money: { price_usd: 20, subs: null, voice: null },
  heard_from: null,
  setup: null,
  time_to_first_task: [],
  signals: { stopped: null, automations: null, features: null, crash: null, versions: null, updates: null },
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
    expect(screen.getByText("2026-10 (partial)")).toBeTruthy();
    expect(screen.getByText("$40")).toBeTruthy();
    expect(screen.getAllByText("1 / 4 (25%)")).toHaveLength(2); // trial to paid, under 5 min
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

  it("renders the signals as counts, k / n, and the stop split", () => {
    render(<Signals data={DATA} />);
    expect(screen.getByText(/Stopped 3 \/ started 12 \(25%\)/)).toBeTruthy(); // stopped / started
    expect(screen.getByText(/pressed 2, said 1, other 0/)).toBeTruthy();
    expect(screen.getByText(/started 8 \/ 10 \(80%\)/)).toBeTruthy(); // automations started
    expect(screen.getByText(/with a brain 7 \/ 10 \(70%\)/)).toBeTruthy(); // ... with a brain
    expect(screen.queryByText("3 / 7 (43%)")).toBeNull(); // features are plain people counts
    expect(screen.getByText(/2 \/ 7 \(29%\)/)).toBeTruthy(); // crashed people / active people
    expect(screen.getByText("1.1")).toBeTruthy();
    expect(screen.queryByText(/succeeded/)).toBeNull();
  });

  it("shows stops without a percentage when they exceed the starts", () => {
    const d = { ...DATA, signals: { ...DATA.signals, stopped: { k: 4, n: 1, how: [{ key: "other", count: 4 }] } } };
    render(<Signals data={d} />);
    expect(screen.getByText(/Stopped 4 \/ started 1/)).toBeTruthy();
    expect(screen.queryByText(/400%/)).toBeNull();
  });

  it("renders each versions and crashes part on its own", () => {
    const d = { ...DATA, signals: { ...DATA.signals, versions: null, crash: null } };
    render(<Signals data={d} />);
    expect(screen.getByText("1.1")).toBeTruthy(); // updates still show
  });

  it("shows no data yet for signals never seen", () => {
    render(<Signals data={EMPTY} />);
    expect(screen.getAllByText("no data yet").length).toBeGreaterThanOrEqual(4);
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
