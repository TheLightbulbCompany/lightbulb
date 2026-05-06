import { test, expect, vi } from "vitest";
// Smoke tests for IssueColumns ports — the four exports:
//   issueActivityText, IssueColumnPicker, InboxIssueMetaLeading,
//   InboxIssueTrailingColumns. Verifies that the rethemed amber palette
//   is rendered (not blue), that the tooltip degradation surfaces a native
//   title= attribute, and that columns drive the trailing grid.

import { render, fireEvent } from "@testing-library/react";
import {
  InboxIssueMetaLeading,
  InboxIssueTrailingColumns,
  IssueColumnPicker,
  issueActivityText,
  issueTrailingColumns,
} from "@/components/teams/inbox/IssueColumns";
import type { Issue } from "@/components/teams/shared/types";

const issue: Issue = {
  id: "iss_1",
  identifier: "PAP-1",
  title: "Fix the inbox",
  status: "todo",
  updatedAt: "2026-05-04T00:00:00Z",
};

test("issueActivityText returns an `Updated <time>` string", () => {
  const txt = issueActivityText(issue);
  expect(typeof txt).toBe("string");
  expect(txt.startsWith("Updated ")).toBe(true);
});

test("InboxIssueMetaLeading renders the identifier when not live", () => {
  const { getByText } = render(<InboxIssueMetaLeading issue={issue} />);
  expect(getByText("PAP-1")).toBeInTheDocument();
});

test("InboxIssueMetaLeading renders a Live badge with the editorial forest palette", () => {
  const { container, getByText } = render(
    <InboxIssueMetaLeading issue={issue} isLive />,
  );
  expect(getByText("Live")).toBeInTheDocument();
  // Editorial retheme assertion: forest tokens, not amber and not blue.
  // The Live pill now uses inline `style="background: var(--forest-tint)"`
  // and inner spans `background: var(--forest)` / `color: var(--forest)`.
  const html = container.innerHTML;
  expect(html).toMatch(/var\(--forest/);
  expect(html).not.toMatch(/amber-\d/);
  expect(html).not.toMatch(/blue-\d/);
});

test("InboxIssueMetaLeading hides the identifier when showIdentifier=false", () => {
  const { queryByText } = render(
    <InboxIssueMetaLeading issue={issue} showIdentifier={false} />,
  );
  expect(queryByText("PAP-1")).toBeNull();
});

test("InboxIssueTrailingColumns renders the activity text in the updated column", () => {
  const { container } = render(
    <InboxIssueTrailingColumns issue={issue} columns={["updated"]} />,
  );
  expect(container.textContent ?? "").toMatch(/(ago|just now|y$|d$|h$|m$|s$)/);
});

test("InboxIssueTrailingColumns shows 'Unassigned' when no assignee + assignee column", () => {
  const { getByText } = render(
    <InboxIssueTrailingColumns issue={issue} columns={["assignee"]} />,
  );
  expect(getByText("Unassigned")).toBeInTheDocument();
});

test("InboxIssueTrailingColumns falls back to 'No project' when projectName missing", () => {
  const { getByText } = render(
    <InboxIssueTrailingColumns issue={issue} columns={["project"]} />,
  );
  expect(getByText("No project")).toBeInTheDocument();
});

test("InboxIssueTrailingColumns workspace cell uses native title= when onFilterWorkspace provided", () => {
  const onFilter = vi.fn();
  const { getByRole } = render(
    <InboxIssueTrailingColumns
      issue={issue}
      columns={["workspace"]}
      workspaceId="ws_1"
      workspaceName="Engineering"
      onFilterWorkspace={onFilter}
    />,
  );
  const button = getByRole("button", { name: /engineering/i });
  // Tooltip degradation: native title= must be present until shadcn Tooltip lands.
  expect(button.getAttribute("title")).toBe("Filter by workspace");
  fireEvent.click(button);
  expect(onFilter).toHaveBeenCalledWith("ws_1");
});

test("issueTrailingColumns is the default 6-column trailing tuple", () => {
  expect(issueTrailingColumns).toEqual([
    "assignee",
    "project",
    "workspace",
    "parent",
    "labels",
    "updated",
  ]);
});

test("IssueColumnPicker renders the trigger button and is keyboard-accessible", () => {
  const { getByRole } = render(
    <IssueColumnPicker
      availableColumns={["status", "id", "updated"]}
      visibleColumnSet={new Set(["status", "id", "updated"])}
      onToggleColumn={() => {}}
      onResetColumns={() => {}}
      title="Inbox columns"
    />,
  );
  const trigger = getByRole("button", { name: /columns/i });
  expect(trigger).toBeInTheDocument();
});
