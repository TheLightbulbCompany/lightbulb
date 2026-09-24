import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import type { CompanyAgent, Issue, RunDetail } from "@/components/teams/shared/types";

const { data, reads } = vi.hoisted(() => ({
  data: {} as Record<string, unknown>,
  reads: [] as Array<string | null>,
}));

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string | null) => {
      reads.push(path);
      return { data: path ? data[path] : undefined, isLoading: false, error: null, mutate: vi.fn() };
    },
  }),
}));

import { AgentDashboardTab } from "@/components/teams/agents/AgentDashboardTab";
import { AgentRunsTab } from "@/components/teams/agents/AgentRunsTab";

const CEO: CompanyAgent = { id: "agent_ceo", name: "CEO", role: "ceo", title: "Chief", status: "idle" };
const ENG: CompanyAgent = {
  id: "agent_eng",
  name: "Engineering",
  role: "engineer",
  title: "Builds things",
  status: "running",
  reportsTo: "agent_ceo",
  capabilities: "Ships **features**",
};
const ISSUE: Issue = { id: "iss_1", identifier: "ISO-7", title: "Fix the login", status: "in_progress" };
const RUNS: RunDetail[] = [
  {
    id: "run_old_0000",
    status: "succeeded",
    invocationSource: "timer",
    createdAt: "2026-09-20T00:00:00.000Z",
    resultJson: { summary: "Old summary" },
    usageJson: { inputTokens: 1000, outputTokens: 500 },
  },
  {
    id: "run_live_000",
    status: "running",
    invocationSource: "assignment",
    createdAt: "2026-09-24T00:00:00.000Z",
    contextSnapshot: { issueId: "iss_1" },
  },
];

beforeEach(() => {
  for (const k of Object.keys(data)) delete data[k];
  reads.length = 0;
});

describe("AgentDashboardTab (ported stock agent Overview)", () => {
  it("renders the live run, identity stats, capabilities and recent tasks from data", () => {
    data["/agents/agent_eng/runs"] = { runs: RUNS };
    data["/issues?participantAgentId=agent_eng"] = { issues: [ISSUE] };
    render(<AgentDashboardTab agent={ENG} agents={[CEO, ENG]} />);

    // Live run wins over the newer-sorted list's head, heading links to the run
    // panel, the row resolves the snapshot's task and links to it.
    expect(screen.getByText("Live Run")).toBeInTheDocument();
    expect(screen.getByText("Live Run").closest("a")).toHaveAttribute(
      "href",
      "/chat?panel=teams-run-detail&detailId=run_live_000",
    );
    expect(screen.getAllByText("Fix the login")[0].closest("a")).toHaveAttribute(
      "href",
      "/chat?panel=teams-issue-detail&detailId=ISO-7",
    );
    // No fallback fetch: the task was already on the loaded page.
    expect(reads).not.toContain("/issues/iss_1");

    const identity = screen.getByRole("region", { name: "Identity" });
    expect(within(identity).getByText("Engineer")).toBeInTheDocument();
    expect(within(identity).getByText("Builds things")).toBeInTheDocument();
    expect(within(identity).getByText("CEO").closest("a")).toHaveAttribute(
      "href",
      "/chat?panel=agent-inspector&agent=agent_ceo",
    );
    expect(within(identity).getByText("0")).toBeInTheDocument(); // direct reports
    expect(screen.getByText("features")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Recent Tasks" })).toHaveTextContent("Fix the login");
  });

  it("falls back to the latest run and fetches a task missing from the page", () => {
    data["/agents/agent_ceo/runs"] = { runs: [{ ...RUNS[0], contextSnapshot: { taskId: "iss_9" } }] };
    render(<AgentDashboardTab agent={CEO} agents={[CEO, ENG]} />);
    expect(screen.getByText("Latest Run")).toBeInTheDocument();
    expect(reads).toContain("/issues/iss_9");
    expect(screen.getByRole("region", { name: "Identity" })).toHaveTextContent("Board");
    expect(screen.getByText("No recent tasks.")).toBeInTheDocument();
  });
});

describe("AgentRunsTab (ported stock Runs list)", () => {
  it("lists runs newest first and links each to the run-detail panel", () => {
    data["/agents/agent_eng/runs"] = { runs: RUNS };
    render(<AgentRunsTab agentId="agent_eng" />);
    const links = screen.getAllByRole("link");
    expect(links.map((l) => l.getAttribute("href"))).toEqual([
      "/chat?panel=teams-run-detail&detailId=run_live_000",
      "/chat?panel=teams-run-detail&detailId=run_old_0000",
    ]);
    expect(links[1]).toHaveTextContent("Timer");
    expect(links[1]).toHaveTextContent("Old summary");
    expect(links[1]).toHaveTextContent("1.5k tok");
    expect(links[0]).toHaveTextContent("Assignment");
  });

  it("says so when there are no runs", () => {
    data["/agents/agent_eng/runs"] = { runs: [] };
    render(<AgentRunsTab agentId="agent_eng" />);
    expect(screen.getByText("No runs yet.")).toBeInTheDocument();
  });
});
