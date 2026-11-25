import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { SparkData } from "../../_lib/api";
import { Channels, Cohorts, Funnel, Headline, People, Reliability, kn } from "../sections";

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
};

describe("spark sections", () => {
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
