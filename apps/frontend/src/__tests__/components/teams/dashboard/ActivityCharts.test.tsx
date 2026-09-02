// Tests for ActivityCharts: ChartCard wrapper, getLast14Days helper, and the
// 2 chart components (RunActivity / SuccessRate).
// Empty-state copy + rendered bar segments are verified per component.
//
// Color assertions use the editorial palette tokens (--midnight, --warn, --midnight-muted,
// etc.) which the chart components emit via inline style attributes.

import { describe, test, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import {
  ChartCard,
  RunActivityChart,
  SuccessRateChart,
  getLast14Days,
} from "@/components/teams/dashboard/ActivityCharts";
import type { DashboardRunActivityDay } from "@/components/teams/shared/types";

describe("getLast14Days", () => {
  test("returns 14 ISO date strings ending today", () => {
    const days = getLast14Days();
    expect(days).toHaveLength(14);
    expect(days[13]).toBe(new Date().toISOString().slice(0, 10));
  });

  test("returns strictly increasing YYYY-MM-DD dates", () => {
    const days = getLast14Days();
    for (let i = 1; i < days.length; i++) {
      expect(days[i] > days[i - 1]).toBe(true);
      expect(days[i]).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    }
  });
});

describe("ChartCard", () => {
  test("renders title + subtitle + children", () => {
    render(
      <ChartCard title="Test" subtitle="14d">
        <div>child</div>
      </ChartCard>
    );
    expect(screen.getByText("Test")).toBeInTheDocument();
    expect(screen.getByText("14d")).toBeInTheDocument();
    expect(screen.getByText("child")).toBeInTheDocument();
  });

  test("renders without subtitle when omitted", () => {
    render(
      <ChartCard title="OnlyTitle">
        <span>kid</span>
      </ChartCard>
    );
    expect(screen.getByText("OnlyTitle")).toBeInTheDocument();
    expect(screen.getByText("kid")).toBeInTheDocument();
  });
});

describe("RunActivityChart", () => {
  test("renders 'No runs yet' when activity is empty", () => {
    render(<RunActivityChart activity={[]} />);
    expect(screen.getByText(/no runs yet/i)).toBeInTheDocument();
  });

  test("renders bar segments for days with runs (succeeded + failed tokens)", () => {
    const today = new Date().toISOString().slice(0, 10);
    const activity: DashboardRunActivityDay[] = [
      { date: today, succeeded: 3, failed: 1, other: 0, total: 4 },
    ];
    const { container } = render(<RunActivityChart activity={activity} />);
    const succeededBar = container.querySelector('[style*="var(--midnight)"]');
    const failedBar = container.querySelector('[style*="var(--warn)"]');
    expect(succeededBar).not.toBeNull();
    expect(failedBar).not.toBeNull();
  });

  test("renders 'other' segment when other > 0 (mute token)", () => {
    const today = new Date().toISOString().slice(0, 10);
    const activity: DashboardRunActivityDay[] = [
      { date: today, succeeded: 0, failed: 0, other: 2, total: 2 },
    ];
    const { container } = render(<RunActivityChart activity={activity} />);
    const otherBar = container.querySelector('[style*="var(--midnight-muted)"]');
    expect(otherBar).not.toBeNull();
  });
});

describe("SuccessRateChart", () => {
  test("renders 'No runs yet' when no activity", () => {
    render(<SuccessRateChart activity={[]} />);
    expect(screen.getByText(/no runs yet/i)).toBeInTheDocument();
  });

  test("computes success rate per day and uses the ok token for >= 80%", () => {
    const today = new Date().toISOString().slice(0, 10);
    const activity: DashboardRunActivityDay[] = [
      { date: today, succeeded: 8, failed: 2, other: 0, total: 10 },
    ];
    const { container } = render(<SuccessRateChart activity={activity} />);
    // 80% rate -> ok token
    const okBar = container.querySelector('[style*="var(--ok)"]');
    expect(okBar).not.toBeNull();
    // tooltip should encode the percentage
    const tooltipNode = container.querySelector('[title*="80%"]');
    expect(tooltipNode).not.toBeNull();
  });

  test("uses the err token for low success rate (< 50%)", () => {
    const today = new Date().toISOString().slice(0, 10);
    const activity: DashboardRunActivityDay[] = [
      { date: today, succeeded: 1, failed: 9, other: 0, total: 10 },
    ];
    const { container } = render(<SuccessRateChart activity={activity} />);
    const lowBar = container.querySelector('[style*="var(--err)"]');
    expect(lowBar).not.toBeNull();
    expect(container.querySelector('[style*="var(--ok)"]')).toBeNull();
  });
});
