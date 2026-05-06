// Tests for ActivityCharts: ChartCard wrapper, getLast14Days helper, and the
// 4 chart components (RunActivity / Priority / IssueStatus / SuccessRate).
// Empty-state copy + rendered bar segments are verified per component.
//
// Color assertions use the editorial palette tokens (--forest, --warn, --mute,
// etc.) which the chart components emit via inline style attributes.

import { describe, test, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import {
  ChartCard,
  RunActivityChart,
  PriorityChart,
  IssueStatusChart,
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
    const succeededBar = container.querySelector('[style*="var(--forest)"]');
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
    const otherBar = container.querySelector('[style*="var(--mute)"]');
    expect(otherBar).not.toBeNull();
  });
});

describe("PriorityChart", () => {
  test("renders 'No issues' when no issues", () => {
    render(<PriorityChart issues={[]} />);
    expect(screen.getByText(/no issues/i)).toBeInTheDocument();
  });

  test("groups by priority over the last 14 days", () => {
    const today = new Date();
    const issues = [
      { priority: "critical", createdAt: today },
      { priority: "high", createdAt: today },
    ];
    const { container } = render(<PriorityChart issues={issues} />);
    // critical -> warn, high -> forest in the editorial mapping
    const criticalSegment = container.querySelector('[style*="var(--warn)"]');
    const highSegment = container.querySelector('[style*="var(--forest)"]');
    expect(criticalSegment || highSegment).not.toBeNull();
  });

  test("renders priority legend labels", () => {
    const today = new Date();
    const issues = [{ priority: "low", createdAt: today }];
    render(<PriorityChart issues={issues} />);
    expect(screen.getByText("Critical")).toBeInTheDocument();
    expect(screen.getByText("High")).toBeInTheDocument();
    expect(screen.getByText("Medium")).toBeInTheDocument();
    expect(screen.getByText("Low")).toBeInTheDocument();
  });
});

describe("IssueStatusChart", () => {
  test("renders 'No issues' when no issues", () => {
    render(<IssueStatusChart issues={[]} />);
    expect(screen.getByText(/no issues/i)).toBeInTheDocument();
  });

  test("renders status legend for present statuses", () => {
    const today = new Date();
    const issues = [
      { status: "todo", createdAt: today },
      { status: "done", createdAt: today },
    ];
    render(<IssueStatusChart issues={issues} />);
    expect(screen.getByText("To Do")).toBeInTheDocument();
    expect(screen.getByText("Done")).toBeInTheDocument();
  });

  test("groups by status and renders bar segments", () => {
    const today = new Date();
    const issues = [
      { status: "in_progress", createdAt: today },
      { status: "in_progress", createdAt: today },
      { status: "blocked", createdAt: today },
    ];
    const { container } = render(<IssueStatusChart issues={issues} />);
    expect(container.firstChild).toBeTruthy();
    // Should not show the "in_review" status legend since none of those issues exist
    expect(screen.queryByText("In Review")).not.toBeInTheDocument();
    expect(screen.getByText("In Progress")).toBeInTheDocument();
    expect(screen.getByText("Blocked")).toBeInTheDocument();
  });
});

describe("SuccessRateChart", () => {
  test("renders 'No runs yet' when no activity", () => {
    render(<SuccessRateChart activity={[]} />);
    expect(screen.getByText(/no runs yet/i)).toBeInTheDocument();
  });

  test("computes success rate per day and uses forest token for >= 80%", () => {
    const today = new Date().toISOString().slice(0, 10);
    const activity: DashboardRunActivityDay[] = [
      { date: today, succeeded: 8, failed: 2, other: 0, total: 10 },
    ];
    const { container } = render(<SuccessRateChart activity={activity} />);
    // 80% rate -> forest token
    const forestBar = container.querySelector('[style*="var(--forest)"]');
    expect(forestBar).not.toBeNull();
    // tooltip should encode the percentage
    const tooltipNode = container.querySelector('[title*="80%"]');
    expect(tooltipNode).not.toBeNull();
  });

  test("uses ink token for low success rate (< 50%)", () => {
    const today = new Date().toISOString().slice(0, 10);
    const activity: DashboardRunActivityDay[] = [
      { date: today, succeeded: 1, failed: 9, other: 0, total: 10 },
    ];
    const { container } = render(<SuccessRateChart activity={activity} />);
    const lowBar = container.querySelector('[style*="var(--ink)"]');
    expect(lowBar).not.toBeNull();
  });
});
