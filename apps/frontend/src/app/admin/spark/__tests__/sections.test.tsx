import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { SparkData } from "../../_lib/api";
import { Cohorts, Failures, Funnel, Headline, pct } from "../sections";

const EMPTY: SparkData = {
  generated_at: "2026-11-25T12:00:00Z",
  weeks: ["2026-11-23"],
  wad: { rolling_7d: 0, weekly: [{ week: "2026-11-23", doers: 0 }] },
  platforms: [],
  cohorts: [
    { week: "2026-11-23", installs: 0, activated: 0, activation_rate: null, w4_retained: null, w4_retention: null, activated_by_platform: {} },
  ],
  task_success: { weekly: [{ week: "2026-11-23", completed: 0, failed: 0, rate: null }], top_failed_reasons: [], top_problem_kinds: [] },
  funnel: [{ stage: "app_first_launch", users: 0 }],
  reliability: { unhealthy_installs_24h: 0 },
  failed_queries: [],
};

describe("spark sections", () => {
  it("renders a share with no denominator as an em dash", () => {
    expect(pct(null)).toBe("—");
    expect(pct(0.5)).toBe("50%");
  });

  it("renders the empty state without crashing", () => {
    render(
      <>
        <Headline data={EMPTY} />
        <Funnel data={EMPTY} />
        <Cohorts data={EMPTY} />
        <Failures data={EMPTY} />
      </>,
    );
    expect(screen.getByText("Weekly Active Doers")).toBeTruthy();
    expect(screen.getAllByText("none").length).toBeGreaterThan(0);
  });

  it("marks young cohorts as too early for retention", () => {
    const data = {
      ...EMPTY,
      platforms: ["mac"],
      cohorts: [{ ...EMPTY.cohorts[0], installs: 4, activated: 2, activation_rate: 0.5, activated_by_platform: { mac: 2 } }],
    };
    render(<Cohorts data={data} />);
    expect(screen.getByText("too early")).toBeTruthy();
    expect(screen.getByText("mac 2")).toBeTruthy();
  });
});
