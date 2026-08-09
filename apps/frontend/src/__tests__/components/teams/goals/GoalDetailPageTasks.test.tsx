// The goal page's Tasks section resolves its rows by filtering the whole
// company issue list on `goalId` — upstream's IssueFilters has no goalId, so
// there is no server-side filter to ask for. That fetch is company-wide and
// lands well after the single-row goal fetch, and an unresolved SWR read
// normalizes to [] — indistinguishable from "this goal has no tasks".
//
// The page's full-view spinner is gated on `isLoading && !goal`, so it stops
// covering for the section the moment the goal arrives. These guard that the
// section says "Loading…" until the issues fetch actually lands, and that it
// only claims "No tasks" once it knows.
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import type { Goal, Issue } from "@/components/teams/shared/types";

const GOAL: Goal = {
  id: "goal_1",
  title: "Ship the thing",
  status: "active",
  level: "company",
} as Goal;

let issuesState: { data: unknown; isLoading: boolean } = {
  data: undefined,
  isLoading: true,
};

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string) => {
      if (path === "/goals/goal_1")
        return { data: GOAL, isLoading: false, error: null, mutate: vi.fn() };
      if (path === "/goals")
        return {
          data: { goals: [GOAL] },
          isLoading: false,
          error: null,
          mutate: vi.fn(),
        };
      if (path === "/projects")
        return {
          data: { projects: [] },
          isLoading: false,
          error: null,
          mutate: vi.fn(),
        };
      if (path.startsWith("/issues"))
        return { ...issuesState, error: null, mutate: vi.fn() };
      return { data: undefined, isLoading: false, error: null, mutate: vi.fn() };
    },
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  }),
}));

vi.mock("@/components/teams/goals/hooks/useGoalMutations", () => ({
  useGoalMutations: () => ({ update: vi.fn(), remove: vi.fn() }),
}));

import { GoalDetailPage } from "@/components/teams/goals/GoalDetailPage";

describe("GoalDetailPage tasks section", () => {
  it("says Loading, not 'No tasks', while the issues fetch is in flight", () => {
    issuesState = { data: undefined, isLoading: true };
    render(<GoalDetailPage goalId="goal_1" />);

    // The goal itself has resolved, so the page body is rendered (the title
    // shows in both breadcrumb and header) — this is exactly the window where
    // an unguarded section asserts an empty result.
    expect(screen.getAllByText("Ship the thing").length).toBeGreaterThan(0);
    expect(screen.getByText(/^Loading…$/)).toBeInTheDocument();
    expect(screen.queryByText(/No tasks\./)).not.toBeInTheDocument();
  });

  it("claims 'No tasks' only once the fetch has landed empty", () => {
    issuesState = { data: { issues: [] }, isLoading: false };
    render(<GoalDetailPage goalId="goal_1" />);
    expect(screen.getByText(/No tasks\./)).toBeInTheDocument();
  });

  it("lists only the tasks linked to this goal", () => {
    const issues: Issue[] = [
      {
        id: "i1",
        title: "Linked task",
        status: "todo",
        identifier: "ISO-1",
        goalId: "goal_1",
      },
      { id: "i2", title: "Other goal task", status: "todo", goalId: "goal_2" },
      { id: "i3", title: "Unlinked task", status: "todo", goalId: null },
    ] as Issue[];
    issuesState = { data: { issues }, isLoading: false };
    render(<GoalDetailPage goalId="goal_1" />);

    expect(screen.getByText("Linked task")).toBeInTheDocument();
    expect(screen.getByText("ISO-1")).toBeInTheDocument();
    expect(screen.queryByText("Other goal task")).not.toBeInTheDocument();
    expect(screen.queryByText("Unlinked task")).not.toBeInTheDocument();
    expect(screen.getByText(/Tasks \(1\)/)).toBeInTheDocument();
  });
});
