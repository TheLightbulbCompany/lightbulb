import { describe, test, expect } from "vitest";
import {
  applyIssueFilters,
  countActiveIssueFilters,
  defaultIssueFilterState,
  issueFilterArraysEqual,
  issueFilterLabel,
  issuePriorityOrder,
  issueQuickFilterPresets,
  issueStatusOrder,
  toggleIssueFilterValue,
  type IssueFilterState,
} from "@/components/teams/shared/lib/issueFilters";
import type { Issue } from "@/components/teams/shared/types";

function makeIssue(partial: Partial<Issue> & Pick<Issue, "id">): Issue {
  return { title: partial.id, status: "todo", ...partial };
}

describe("defaultIssueFilterState", () => {
  test("is fully empty / inactive", () => {
    expect(defaultIssueFilterState).toEqual({
      statuses: [],
      priorities: [],
      assignees: [],
      creators: [],
      labels: [],
      projects: [],
      workspaces: [],
      liveOnly: false,
      hideRoutineExecutions: false,
    } satisfies IssueFilterState);
  });
});

describe("issueFilterLabel", () => {
  test("title-cases snake_case statuses", () => {
    expect(issueFilterLabel("in_progress")).toBe("In Progress");
    expect(issueFilterLabel("blocked")).toBe("Blocked");
  });
});

describe("issueFilterArraysEqual", () => {
  test("treats order-independent arrays as equal", () => {
    expect(issueFilterArraysEqual(["a", "b"], ["b", "a"])).toBe(true);
  });

  test("treats different-length arrays as unequal", () => {
    expect(issueFilterArraysEqual(["a"], ["a", "b"])).toBe(false);
  });

  test("treats different-content arrays as unequal", () => {
    expect(issueFilterArraysEqual(["a", "b"], ["a", "c"])).toBe(false);
  });
});

describe("toggleIssueFilterValue", () => {
  test("adds when missing", () => {
    expect(toggleIssueFilterValue(["a"], "b")).toEqual(["a", "b"]);
  });

  test("removes when present", () => {
    expect(toggleIssueFilterValue(["a", "b"], "a")).toEqual(["b"]);
  });
});

describe("ordering + presets exported", () => {
  test("issueStatusOrder + issuePriorityOrder are non-empty", () => {
    expect(issueStatusOrder.length).toBeGreaterThan(0);
    expect(issuePriorityOrder.length).toBeGreaterThan(0);
  });

  test("issueQuickFilterPresets includes the 'All' preset with empty statuses", () => {
    const all = issueQuickFilterPresets.find((p) => p.label === "All");
    expect(all).toBeDefined();
    expect(all?.statuses).toEqual([]);
  });
});

describe("countActiveIssueFilters", () => {
  test("default state has no active facets", () => {
    expect(countActiveIssueFilters(defaultIssueFilterState)).toBe(0);
  });

  test("counts distinct facets, not the sum of selected values", () => {
    const state: IssueFilterState = {
      ...defaultIssueFilterState,
      statuses: ["todo", "done"],
      priorities: ["high"],
      liveOnly: true,
    };
    // 2 statuses + 1 priority + liveOnly → 3 facets (NOT 4 values).
    expect(countActiveIssueFilters(state)).toBe(3);
  });
});

describe("applyIssueFilters", () => {
  const issues: Issue[] = [
    makeIssue({ id: "a", status: "todo", priority: "high", assigneeAgentId: "ag1" }),
    makeIssue({ id: "b", status: "done", priority: "low" }),
    makeIssue({
      id: "c",
      status: "todo",
      labels: [{ id: "l1", name: "bug" }],
      project: { id: "p1", name: "Core" },
    }),
  ];

  test("returns all issues when the state is empty", () => {
    expect(applyIssueFilters(issues, defaultIssueFilterState)).toHaveLength(3);
  });

  test("filters by status", () => {
    const result = applyIssueFilters(issues, {
      ...defaultIssueFilterState,
      statuses: ["done"],
    });
    expect(result.map((i) => i.id)).toEqual(["b"]);
  });

  test("filters by priority", () => {
    const result = applyIssueFilters(issues, {
      ...defaultIssueFilterState,
      priorities: ["high"],
    });
    expect(result.map((i) => i.id)).toEqual(["a"]);
  });

  test("filters by label id (nested objects, not flat ids)", () => {
    const result = applyIssueFilters(issues, {
      ...defaultIssueFilterState,
      labels: ["l1"],
    });
    expect(result.map((i) => i.id)).toEqual(["c"]);
  });

  test("filters by project id", () => {
    const result = applyIssueFilters(issues, {
      ...defaultIssueFilterState,
      projects: ["p1"],
    });
    expect(result.map((i) => i.id)).toEqual(["c"]);
  });

  test("__unassigned matches issues with no assignee", () => {
    const result = applyIssueFilters(issues, {
      ...defaultIssueFilterState,
      assignees: ["__unassigned"],
    });
    expect(result.map((i) => i.id).sort()).toEqual(["b", "c"]);
  });

  test("liveOnly keeps only issues in the supplied live-id set", () => {
    const result = applyIssueFilters(
      issues,
      { ...defaultIssueFilterState, liveOnly: true },
      { liveIssueIds: new Set(["a"]) },
    );
    expect(result.map((i) => i.id)).toEqual(["a"]);
  });
});
