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
    expect(alert).toHaveTextContent(/failed to load routine/i);
    expect(alert).toHaveTextContent(/boom/i);
  });

  it("renders the not-found state when the routine is absent and no error", () => {
    reads["/routines/r1"] = { data: undefined, isLoading: false, error: undefined };
    render(<RoutineDetailPanel routineId="r1" />);
    expect(screen.getByText(/routine not found/i)).toBeInTheDocument();
  });

  it("renders the header, status swatch + Properties aside, and run history", () => {
    reads["/routines/r1"] = {
      data: {
        id: "r1",
        title: "Nightly digest",
        status: "active",
        assigneeAgentId: "ag_1",
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
      data: { agents: [{ id: "ag_1", name: "Friday" }] },
      isLoading: false,
      error: undefined,
    };
    render(<RoutineDetailPanel routineId="r1" />);

    // Header + breadcrumb.
    expect(
      screen.getByRole("heading", { name: "Nightly digest" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Routines" })).toHaveAttribute(
      "href",
      "/teams/routines",
    );

    // Status swatch surfaces a real label (not an --edge pill).
    const propLabels = screen.getAllByText(/^(Status|Assignee|Created|Updated)$/);
    expect(propLabels.length).toBe(4);
    expect(screen.getByText("Active")).toBeInTheDocument();

    // Assignee resolves to the agent name via the /agents roster.
    expect(screen.getByRole("link", { name: "Friday" })).toHaveAttribute(
      "href",
      "/teams/agents/ag_1",
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
