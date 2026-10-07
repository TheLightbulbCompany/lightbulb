// apps/frontend/src/__tests__/teams/IssueRunsSection.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

const read = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read }) }));

import { IssueRunsSection } from "@/components/teams/issues/IssueRunsSection";

function run(runId: string, agentId: string | null, status: string, startedAt: string) {
  return { runId, agentId, status, startedAt, createdAt: startedAt };
}

beforeEach(() => read.mockReset());

describe("IssueRunsSection", () => {
  it("renders nothing when there are no runs", () => {
    read.mockReturnValue({ data: { runs: [] }, isLoading: false, error: null, mutate: vi.fn() });
    const { container } = render(<IssueRunsSection issueId="iss1" agents={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders runs newest-first with agent name and status", () => {
    read.mockReturnValue({
      data: { runs: [
        run("r-old", "ag1", "succeeded", "2026-05-01T00:00:00Z"),
        run("r-new", "ag1", "failed", "2026-05-03T00:00:00Z"),
      ] },
      isLoading: false, error: null, mutate: vi.fn(),
    });
    render(<IssueRunsSection issueId="iss1" agents={[{ id: "ag1", name: "Atlas" } as never]} />);
    expect(screen.getByText(/Runs/)).toBeInTheDocument();
    // Newest first: the failed r-new row renders above the succeeded r-old row.
    const statuses = screen
      .getAllByRole("listitem")
      .map((li) => li.textContent ?? "");
    expect(statuses[0]).toMatch(/failed/i);
    expect(statuses[1]).toMatch(/succeeded/i);
    expect(screen.getAllByText(/Atlas/).length).toBeGreaterThan(0);
  });

  it("links each run row to the run-detail panel", () => {
    read.mockReturnValue({
      data: { runs: [run("run_1", "ag1", "succeeded", "2026-05-01T00:00:00Z")] },
      isLoading: false, error: null, mutate: vi.fn(),
    });
    render(<IssueRunsSection issueId="iss1" agents={[{ id: "ag1", name: "Atlas" } as never]} />);
    const link = screen.getByRole("link", { name: /view run/i });
    expect(link).toHaveAttribute(
      "href",
      "/chat?panel=teams-run-detail&detailId=run_1",
    );
  });
});
