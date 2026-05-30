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

  it("renders runs newest-first with agent name, status, and a View link", () => {
    read.mockReturnValue({
      data: { runs: [
        run("r-old", "ag1", "succeeded", "2026-05-01T00:00:00Z"),
        run("r-new", "ag1", "failed", "2026-05-03T00:00:00Z"),
      ] },
      isLoading: false, error: null, mutate: vi.fn(),
    });
    render(<IssueRunsSection issueId="iss1" agents={[{ id: "ag1", name: "Atlas" } as never]} />);
    expect(screen.getByText(/Runs/)).toBeInTheDocument();
    const links = screen.getAllByRole("link", { name: /view/i });
    expect(links[0]).toHaveAttribute("href", "/teams/agents/ag1/runs/r-new"); // newest first
    expect(links[1]).toHaveAttribute("href", "/teams/agents/ag1/runs/r-old");
    expect(screen.getAllByText(/Atlas/).length).toBeGreaterThan(0);
    expect(screen.getByText(/failed/i)).toBeInTheDocument();
  });

  it("hides the View link when a run has no agentId", () => {
    read.mockReturnValue({
      data: { runs: [run("r1", null, "succeeded", "2026-05-01T00:00:00Z")] },
      isLoading: false, error: null, mutate: vi.fn(),
    });
    render(<IssueRunsSection issueId="iss1" agents={[]} />);
    expect(screen.queryByRole("link", { name: /view/i })).toBeNull();
  });
});
