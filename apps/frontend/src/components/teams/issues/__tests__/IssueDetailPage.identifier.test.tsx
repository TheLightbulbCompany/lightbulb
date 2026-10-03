import { describe, it, expect, vi } from "vitest";
import { render } from "@testing-library/react";
import { IssueDetailPage } from "@/components/teams/issues/IssueDetailPage";
import type { Issue } from "@/components/teams/shared/types";

// A Case link can open a task by identifier (INT-7). Paperclip's detail route
// accepts that, but its sub-resource routes need the uuid, and the cache keys
// are uuid-keyed — so every read after the issue loads must use issue.id.
const UUID = "33333333-3333-3333-3333-333333333333";
const ISSUE: Issue = { id: UUID, identifier: "INT-7", title: "Linked task", status: "todo", priority: null };

const { paths } = vi.hoisted(() => ({ paths: [] as string[] }));

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string | null) => {
      if (path) paths.push(path);
      const isDetail = path === "/issues/INT-7" || path === `/issues/${UUID}`;
      return { data: isDetail ? ISSUE : undefined, isLoading: false, error: null, mutate: vi.fn() };
    },
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
    mutate: vi.fn(),
  }),
}));

describe("IssueDetailPage opened by identifier", () => {
  it("reads comments and sub-issues by the issue's uuid", () => {
    render(<IssueDetailPage issueId="INT-7" />);

    expect(paths).toContain(`/issues/${UUID}/comments`);
    expect(paths).toContain(`/issues/${UUID}/children`);
    // Only the detail read itself may use the identifier.
    expect(paths.filter((p) => p.startsWith("/issues/INT-7")).every((p) => p === "/issues/INT-7")).toBe(true);
  });
});
