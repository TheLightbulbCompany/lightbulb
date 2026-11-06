import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn() }) }));

// Mock the three detail panels so we can assert the id prop wiring without
// pulling in their data hooks.
vi.mock("@/components/teams/panels/IssueDetailPanel", () => ({
  IssueDetailPanel: ({ issueId }: { issueId: string }) => (
    <div data-testid="issue-detail">{issueId}</div>
  ),
}));
vi.mock("@/components/teams/panels/RoutineDetailPanel", () => ({
  RoutineDetailPanel: ({ routineId }: { routineId: string }) => (
    <div data-testid="routine-detail">{routineId}</div>
  ),
}));
vi.mock("@/components/teams/panels/ProjectDetailPanel", () => ({
  ProjectDetailPanel: ({ projectId }: { projectId: string }) => (
    <div data-testid="project-detail">{projectId}</div>
  ),
}));
vi.mock("@/components/teams/panels/RunDetailPanel", () => ({
  RunDetailPanel: ({ runId }: { runId: string }) => (
    <div data-testid="run-detail-panel">{runId}</div>
  ),
}));

import { TeamsPanelRouter } from "@/components/teams/TeamsPanelRouter";

describe("TeamsPanelRouter detail branch", () => {
  // Detail panels load via next/dynamic, so they resolve asynchronously —
  // findByTestId retries until the lazy boundary settles.
  it("renders IssueDetailPanel with the issueId for issue-detail + detailId", async () => {
    render(<TeamsPanelRouter panel="issue-detail" detailId="iss_123" />);
    expect(await screen.findByTestId("issue-detail")).toHaveTextContent(
      "iss_123",
    );
  });

  it("renders RoutineDetailPanel with the routineId for routine-detail + detailId", async () => {
    render(<TeamsPanelRouter panel="routine-detail" detailId="rtn_456" />);
    expect(await screen.findByTestId("routine-detail")).toHaveTextContent(
      "rtn_456",
    );
  });

  it("renders ProjectDetailPanel with the projectId for project-detail + detailId", async () => {
    render(<TeamsPanelRouter panel="project-detail" detailId="prj_789" />);
    expect(await screen.findByTestId("project-detail")).toHaveTextContent(
      "prj_789",
    );
  });

  it("renders RunDetailPanel for panel=run-detail", async () => {
    render(<TeamsPanelRouter panel="run-detail" detailId="r1" />);
    // The dynamic chunk resolves async; the mocked RunDetailPanel renders a marker.
    expect(await screen.findByTestId("run-detail-panel")).toBeInTheDocument();
  });

  it("renders Unknown panel when a detail key arrives without a detailId", () => {
    render(<TeamsPanelRouter panel="issue-detail" />);
    expect(screen.queryByTestId("issue-detail")).toBeNull();
    expect(screen.getByText(/This page is no longer here/)).toBeInTheDocument();
  });
});
