import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Issue } from "@/components/teams/shared/types";
import { taskIdentifiers } from "../MessageTaskCard";
import { RoomTasks } from "../RoomTasks";


const teams = vi.hoisted(() => ({
  issues: undefined as Issue[] | undefined,
  error: undefined as Error | undefined,
  paths: [] as (string | null)[],
}));
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string | null) => {
      teams.paths.push(path);
      if (path === "/issues?excludeRoutineExecutions=true") {
        return { data: teams.issues && { issues: teams.issues }, error: teams.error };
      }
      if (path === "/agents") return { data: { agents: [{ id: "a-fin", name: "Finance" }] } };
      return { data: undefined };
    },
  }),
}));
const live = vi.hoisted(() => ({ runs: [] as unknown[] }));
vi.mock("@/components/teams/dashboard/hooks/useLiveRuns", async (orig) => ({
  ...(await orig<typeof import("@/components/teams/dashboard/hooks/useLiveRuns")>()),
  useLiveRuns: () => ({ runs: live.runs, isLoading: false, isError: false }),
}));

const issue = (n: number, extra: Partial<Issue> = {}): Issue => ({
  id: `id-${n}`, identifier: `ISO-${n}`, title: `Task ${n}`, status: "todo", ...extra,
});

beforeEach(() => {
  teams.issues = [issue(14, { status: "in_progress", assigneeAgentId: "a-fin" }), issue(15), issue(16), issue(17)];
  teams.error = undefined;
  teams.paths = [];
  live.runs = [];
});

describe("taskIdentifiers", () => {
  it("finds every identifier once, in order", () => {
    expect(taskIdentifiers("Put ISO-14 and ENG2-7 on it; ISO-14 again.")).toEqual(["ISO-14", "ENG2-7"]);
  });
  it("ignores URLs, code, lowercase and embedded tokens", () => {
    expect(taskIdentifiers(
      "see https://x.dev/ISO-1 and `ISO-2`\n```\nISO-3\n```\nutf-8 abcISO-4 [ISO-5](https://x.dev/ISO-6)",
    )).toEqual(["ISO-5"]);
  });
});

describe("RoomTasks grouping", () => {
  it("files every finished status under Done and open under Up next", () => {
    const tasks = [
      issue(1, { status: "closed" }), issue(2, { status: "won_t_do" }), issue(3, { status: "duplicate" }),
      issue(4, { status: "open" }),
    ];
    teams.issues = tasks;
    render(<RoomTasks tasks={tasks} onOpenAll={vi.fn()} />);
    expect(screen.getByRole("list", { name: "Done" })).toHaveTextContent(/Task 1.*Task 2.*Task 3/);
    expect(screen.getByRole("list", { name: "Up next" })).toHaveTextContent("Task 4");
    expect(screen.queryByRole("list", { name: "In progress" })).not.toBeInTheDocument();
  });
});
