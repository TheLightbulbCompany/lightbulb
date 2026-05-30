import { test, expect, vi } from "vitest";
// Smoke tests for IssueRow — rendering, mark-read, and archive callbacks.
// Upstream's row uses `unreadState` to gate which trailing button shows:
//   "visible" / "fading"  -> mark-as-read dot button (calls onMarkRead())
//   "hidden"              -> archive X button when onArchive provided

import { render, fireEvent } from "@testing-library/react";
import { IssueRow } from "@/components/teams/inbox/IssueRow";
import type { Issue } from "@/components/teams/shared/types";

const issue: Issue = {
  id: "iss_1",
  identifier: "PAP-1",
  title: "Fix the inbox",
  status: "todo",
  unread: true,
};

test("renders the issue title + identifier", () => {
  const { getByText } = render(<IssueRow issue={issue} />);
  expect(getByText(/Fix the inbox/)).toBeInTheDocument();
  expect(getByText(/PAP-1/)).toBeInTheDocument();
});

test("links by internal id, not the human-readable identifier", () => {
  // The detail route /teams/issues/{id} resolves upstream by the internal id
  // ONLY, so a row with identifier "PAP-1" must still link to its id "iss_1".
  // The identifier stays the visible label.
  const { container, getByText } = render(<IssueRow issue={issue} />);
  const link = container.querySelector("a[data-inbox-issue-link]");
  expect(link).not.toBeNull();
  expect(link?.getAttribute("href")).toBe("/teams/issues/iss_1");
  expect(getByText("PAP-1")).toBeInTheDocument();
});

test("renders identifier label while linking by id", () => {
  const { container, getByText } = render(
    <IssueRow
      issue={{ id: "uuid-1", identifier: "ENG-42", title: "T", status: "todo" }}
    />,
  );
  const link = container.querySelector("a[data-inbox-issue-link]");
  expect(link?.getAttribute("href")).toBe("/teams/issues/uuid-1");
  expect(getByText("ENG-42")).toBeInTheDocument();
});

test("unread issue surfaces a mark-as-read button", () => {
  const onMarkRead = vi.fn();
  const { getByRole } = render(
    <IssueRow issue={issue} unreadState="visible" onMarkRead={onMarkRead} />,
  );
  fireEvent.click(getByRole("button", { name: /mark as read/i }));
  expect(onMarkRead).toHaveBeenCalledTimes(1);
});

test("archive button fires onArchive without following the link", () => {
  const onArchive = vi.fn();
  const { getByRole } = render(
    <IssueRow issue={issue} unreadState="hidden" onArchive={onArchive} />,
  );
  fireEvent.click(getByRole("button", { name: /dismiss from inbox/i }));
  expect(onArchive).toHaveBeenCalledTimes(1);
});
