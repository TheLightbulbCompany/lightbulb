import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

const read = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read }) }));

import { RoutineDetailPanel } from "@/components/teams/panels/RoutineDetailPanel";

function mockReads(
  routine: Record<string, unknown>,
  runs: Record<string, unknown>[],
) {
  read.mockImplementation((path: string) => {
    if (path === "/routines/r1")
      return { data: routine, isLoading: false, error: null, mutate: vi.fn() };
    if (path === "/routines/r1/runs")
      return { data: { runs }, isLoading: false, error: null, mutate: vi.fn() };
    return { data: undefined, isLoading: false, error: null, mutate: vi.fn() };
  });
}

beforeEach(() => read.mockReset());

describe("RoutineDetailPanel", () => {
  it("renders the header (title, status, schedule, assignee) and run history newest-first", () => {
    mockReads(
      {
        id: "r1",
        title: "Nightly digest",
        status: "active",
        assignee: { id: "ag1", name: "Atlas" },
        triggers: [
          {
            id: "t1",
            kind: "cron",
            cronExpression: "0 9 * * *",
            timezone: "UTC",
            enabled: true,
          },
        ],
      },
      [
        {
          id: "run-old",
          source: "schedule",
          status: "succeeded",
          triggeredAt: "2026-05-01T00:00:00Z",
        },
        {
          id: "run-new",
          source: "manual",
          status: "failed",
          triggeredAt: "2026-05-03T00:00:00Z",
          failureReason: "boom",
          linkedIssue: { id: "iss9", identifier: "ENG-9" },
        },
      ],
    );
    render(<RoutineDetailPanel routineId="r1" />);
    expect(screen.getByText("Nightly digest")).toBeInTheDocument();
    expect(screen.getByText("active")).toBeInTheDocument();
    expect(screen.getByText("0 9 * * *")).toBeInTheDocument();
    expect(screen.getByText("Atlas")).toBeInTheDocument();
    // run history present; failed run shows its reason + a linked-issue link
    expect(screen.getByText(/failed/i)).toBeInTheDocument();
    expect(screen.getByText("boom")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "ENG-9" })).toHaveAttribute(
      "href",
      "/teams/issues/iss9",
    );
  });

  it("shows 'No runs yet.' when there are no runs", () => {
    mockReads(
      { id: "r1", title: "Empty routine", status: "active", triggers: [] },
      [],
    );
    render(<RoutineDetailPanel routineId="r1" />);
    expect(screen.getByText("Empty routine")).toBeInTheDocument();
    expect(screen.getByText(/no runs yet/i)).toBeInTheDocument();
  });

  it("renders not-found when the routine read is empty", () => {
    read.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: null,
      mutate: vi.fn(),
    });
    render(<RoutineDetailPanel routineId="r1" />);
    expect(screen.getByText(/routine not found/i)).toBeInTheDocument();
  });
});
