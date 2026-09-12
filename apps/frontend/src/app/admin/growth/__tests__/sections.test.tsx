import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { GrowthData, GrowthSegment } from "../../_lib/api";
import { Cohorts, Messaging, pct } from "../sections";

const SEG: GrowthSegment = {
  workspaces: {
    total: 3,
    paying: 1,
    paying_by_plan: { standard: 1 },
    paying_by_provider: { bedrock_claude: 1 },
    trial_active: 1,
    trial_expired: 1,
    signups_by_week: [],
  },
  activation: { activated: 1, measurable: 3, rate: 1 / 3 },
  weekly_active: [{ week: "2026-08-24", workspaces: 2, multiplayer: 1, multiplayer_share: 0.5 }],
  answered: { dispatched: 4, answered: 3, rate: 0.75, p50_reply_s: 12.5 },
  cohorts: [{ week: "2026-08-24", signups: 3, measurable: 0, activated: 0, paying: 0 }],
  retention_w4: { eligible: 0, retained: 0, rate: null },
  messaging: { rooms: 17, human_messages_this_week: 42 },
  daily: { signups: [], human_messages: [], activated: [] },
};

const DATA = { dlq_depth: 0 } as unknown as GrowthData;

describe("growth sections", () => {
  it("renders a share with no denominator as an em dash, never 0%", () => {
    expect(pct(null)).toBe("—");
    expect(pct(undefined)).toBe("—");
    expect(pct(0)).toBe("0%");
    expect(pct(0.756)).toBe("76%");
  });

  it("shows the messaging counters and flags an answered rate under the 95% bar", () => {
    render(<Messaging seg={SEG} data={DATA} />);
    expect(screen.getByText("17")).toBeTruthy();
    expect(screen.getByText("42")).toBeTruthy();
    expect(screen.getByText("12.5s")).toBeTruthy();
    expect(screen.getByText("75%").className).toContain("text-err");
  });

  it("flags a non-empty work DLQ and reports a failed CloudWatch read as unknown, not zero", () => {
    const { rerender, container } = render(<Messaging seg={SEG} data={{ ...DATA, dlq_depth: 4 }} />);
    expect(screen.getByText("4").className).toContain("text-err");

    rerender(<Messaging seg={SEG} data={{ ...DATA, dlq_depth: null }} />);
    expect(screen.getByText("CloudWatch read failed")).toBeTruthy();
    expect(container.querySelectorAll(".text-err")).toHaveLength(1); // only the answered rate
  });

  it("shows a cohort whose 7-day window is still open as — rather than 0% activated", () => {
    render(<Cohorts seg={SEG} />);
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });
});
