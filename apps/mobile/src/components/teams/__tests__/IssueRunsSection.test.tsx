import { render, screen } from "@testing-library/react-native";

// IssueRunsSection imports teamsData, which imports expo-router
// (useFocusEffect) at module level; stub it like the other teamsData tests.
jest.mock("expo-router", () => ({ useFocusEffect: jest.fn() }));

import { IssueRunsSection } from "../IssueRunsSection";
import type { IssueRun, TeamsAgent } from "../../../lib/teamsData";

const agents = [
  { id: "agent_f", name: "Friday" },
  { id: "agent_a", name: "Atlas" },
] as TeamsAgent[];

const runs: IssueRun[] = [
  {
    runId: "run_2",
    agentId: "agent_f",
    status: "running",
    startedAt: "2026-07-20T10:00:00Z",
  },
  {
    runId: "run_1",
    agentId: "agent_a",
    status: "succeeded",
    startedAt: "2026-07-19T09:00:00Z",
  },
];

describe("IssueRunsSection", () => {
  it("renders one row per run with status and agent name, newest first", async () => {
    await render(<IssueRunsSection runs={runs} agents={agents} />);
    expect(screen.getByText("RUNS")).toBeTruthy();
    expect(screen.getByText("running")).toBeTruthy();
    expect(screen.getByText("Friday")).toBeTruthy();
    expect(screen.getByText("succeeded")).toBeTruthy();
    expect(screen.getByText("Atlas")).toBeTruthy();
  });

  it("falls back to the raw agent id when the roster misses it", async () => {
    await render(
      <IssueRunsSection
        runs={[{ runId: "r", agentId: "agent_ghost", status: "failed" }]}
        agents={agents}
      />,
    );
    expect(screen.getByText("agent_ghost")).toBeTruthy();
  });

  it("renders nothing when there are no runs", async () => {
    await render(<IssueRunsSection runs={[]} agents={agents} />);
    expect(screen.queryByText("RUNS")).toBeNull();
  });
});
