import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import type { SWRResponse } from "swr";
import { RoutineDetailPanel } from "../RoutineDetailPanel";

// Per-path read stub: callers drive the routine / runs / agents reads by
// swapping the module-level `reads` map between tests.
type ReadStub = Partial<SWRResponse<unknown, Error>>;
let reads: Record<string, ReadStub> = {};

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string) =>
      reads[path] ?? { data: undefined, isLoading: false, error: undefined },
  }),
}));

beforeEach(() => {
  reads = {};
  vi.clearAllMocks();
});

describe("RoutineDetailPanel", () => {
  it("renders an alert error state when the routine read fails", () => {
    reads["/routines/r1"] = {
      data: undefined,
      isLoading: false,
      error: new Error("boom"),
    };
    render(<RoutineDetailPanel routineId="r1" />);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/failed to load automation/i);
    expect(alert).toHaveTextContent(/boom/i);
  });

  it("renders the not-found state when the routine is absent and no error", () => {
    reads["/routines/r1"] = { data: undefined, isLoading: false, error: undefined };
    render(<RoutineDetailPanel routineId="r1" />);
    expect(screen.getByText(/automation not found/i)).toBeInTheDocument();
  });

  it("renders the header, status swatch + Properties aside, and run history", () => {
    reads["/routines/r1"] = {
      data: {
        id: "r1",
        title: "Nightly digest",
        status: "active",
        assigneeAgentId: "ag_1",
        executionPolicy: {
          mode: "normal",
          stages: [
            {
              type: "review",
              approvalsNeeded: 1,
              participants: [
                { type: "agent", agentId: "ag_2", userId: null },
              ],
            },
          ],
        },
        createdAt: "2026-01-01T00:00:00Z",
        updatedAt: "2026-01-02T00:00:00Z",
        triggers: [
          { id: "t1", kind: "cron", cronExpression: "0 9 * * *", enabled: true },
        ],
      },
      isLoading: false,
      error: undefined,
    };
    reads["/routines/r1/runs"] = {
      data: { runs: [{ id: "run1", source: "cron", status: "succeeded" }] },
      isLoading: false,
      error: undefined,
    };
    reads["/agents"] = {
      data: {
        agents: [
          { id: "ag_1", name: "Friday" },
          { id: "ag_2", name: "Atlas" },
        ],
      },
      isLoading: false,
      error: undefined,
    };
    render(<RoutineDetailPanel routineId="r1" />);

    // Header + breadcrumb.
    expect(
      screen.getByRole("heading", { name: "Nightly digest" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Automations" })).toHaveAttribute(
      "href",
      "/chat?panel=teams-routines",
    );

    // Status swatch surfaces a real label (not an --edge pill).
    const propLabels = screen.getAllByText(
      /^(Status|Assignee|Reviewer|Created|Updated)$/,
    );
    expect(propLabels.length).toBe(5);
    expect(screen.getByText("Active")).toBeInTheDocument();

    // Assignee resolves to the agent name via the /agents roster and links to
    // the shell's per-agent inspector (agents have no teams-* detail panel).
    expect(screen.getByRole("link", { name: "Friday" })).toHaveAttribute(
      "href",
      "/chat?panel=agent-inspector&agent=ag_1",
    );
    expect(screen.getByRole("link", { name: "Atlas" })).toHaveAttribute(
      "href",
      "/chat?panel=agent-inspector&agent=ag_2",
    );

    // Schedule + run history rendered.
    expect(screen.getByText("0 9 * * *")).toBeInTheDocument();
    expect(screen.getByText("succeeded")).toBeInTheDocument();
  });

  it("shows a run-history error when the runs read fails", () => {
    reads["/routines/r1"] = {
      data: { id: "r1", title: "Nightly digest", status: "active", triggers: [] },
      isLoading: false,
      error: undefined,
    };
    reads["/routines/r1/runs"] = {
      data: undefined,
      isLoading: false,
      error: new Error("runs boom"),
    };
    render(<RoutineDetailPanel routineId="r1" />);
    expect(
      screen.getByText(/failed to load run history/i),
    ).toBeInTheDocument();
  });
});
