import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";

vi.mock("@/components/teams/dashboard/hooks/useLiveRuns", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/components/teams/dashboard/hooks/useLiveRuns")>();
  return { ...mod, useLiveRuns: vi.fn() };
});
vi.mock("@/lib/shellNav", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/lib/shellNav")>();
  return { ...mod, handleShellLinkClick: vi.fn() };
});

import { HomeHappeningNow } from "../HomeHappeningNow";
import { useLiveRuns, type LiveRun } from "@/components/teams/dashboard/hooks/useLiveRuns";
import type { Issue } from "@/components/teams/shared/types";

const run = (over: Partial<LiveRun>): LiveRun => ({
  id: "r1", status: "running", agentName: "Friday", issueId: "iss1",
  startedAt: new Date(Date.now() - 4 * 60_000).toISOString(), nextAction: "Drafting the stockist email", ...over,
});
const issues = new Map<string, Issue>([["iss1", { id: "iss1", title: "Launch fall collection" } as Issue]]);

function runs(over: Partial<ReturnType<typeof useLiveRuns>>) {
  vi.mocked(useLiveRuns).mockReturnValue({ runs: [], isLoading: false, isError: false, ...over });
}

describe("HomeHappeningNow", () => {
  beforeEach(() => vi.clearAllMocks());

  it("lists only active runs, names the task, and shows what the agent is doing", () => {
    runs({ runs: [run({}), run({ id: "r2", status: "completed" })] });
    render(<HomeHappeningNow issuesById={issues} openTasks={12} />);
    const section = screen.getByRole("region", { name: "Happening now" });
    const rows = within(section).getAllByRole("link", { name: /Launch fall collection/ });
    expect(rows).toHaveLength(1);
    expect(within(rows[0]).getByText(/Running — Drafting the stockist email/)).toBeInTheDocument();
    expect(within(rows[0]).getByText(/4m ago|4 min/)).toBeInTheDocument();
    expect(within(section).getByRole("link", { name: "Open Tasks · 12 open" })).toHaveAttribute("href", expect.stringContaining("teams-issues"));
  });

  it("marks a stalled run in a word, not only a colour", () => {
    runs({ runs: [run({ outputSilence: { level: "critical", silenceAgeMs: 600_000 } as LiveRun["outputSilence"] })] });
    render(<HomeHappeningNow issuesById={issues} openTasks={1} />);
    expect(screen.getByText("stalled")).toBeInTheDocument();
  });

  it("says queued, not running, for a run that hasn't started", () => {
    runs({ runs: [run({ status: "queued" })] });
    render(<HomeHappeningNow issuesById={issues} openTasks={1} />);
    expect(screen.getByText("queued")).toBeInTheDocument();
    expect(screen.queryByText("running")).not.toBeInTheDocument();
  });

  it("falls back to the agent's own words when the task is unknown", () => {
    runs({ runs: [run({ issueId: "missing" })] });
    render(<HomeHappeningNow issuesById={issues} openTasks={0} />);
    expect(screen.getByRole("link", { name: /Drafting the stockist email/ })).toBeInTheDocument();
  });

  it("caps at four rows", () => {
    runs({ runs: [1, 2, 3, 4, 5].map((n) => run({ id: `r${n}` })) });
    render(<HomeHappeningNow issuesById={issues} openTasks={5} />);
    expect(screen.getAllByRole("link", { name: /Launch fall collection/ })).toHaveLength(4);
  });

  it("is calm when nobody is running, and does not print an unread open count", () => {
    runs({});
    render(<HomeHappeningNow issuesById={issues} openTasks={undefined} />);
    expect(screen.getByRole("status")).toHaveTextContent("Nobody is running right now.");
    expect(screen.getByRole("link", { name: "Open Tasks" })).toBeInTheDocument();
  });

  it("reports a feed failure as unavailable", () => {
    runs({ isError: true });
    render(<HomeHappeningNow issuesById={issues} openTasks={3} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't see what's running just now.");
  });
});
