import { test, expect, vi } from "vitest";
// Smoke tests for IssueRow — rendering, mark-read, and archive callbacks.
// Upstream's row uses `unreadState` to gate which trailing button shows:
//   "visible" / "fading"  -> mark-as-read dot button (calls onMarkRead())
//   "hidden"              -> archive X button when onArchive provided

import { render, fireEvent } from "@testing-library/react";
import { IssueRow } from "@/components/teams/inbox/IssueRow";
import { InlineTeamsNavProvider } from "@/components/chat/InlineTeamsNav";
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
  // The detail view resolves upstream by the internal id ONLY, so a row with
  // identifier "PAP-1" must still link to its id "iss_1". The identifier
  // stays the visible label.
  const { container, getByText } = render(<IssueRow issue={issue} />);
  const link = container.querySelector("a[data-inbox-issue-link]");
  expect(link).not.toBeNull();
  expect(link?.getAttribute("href")).toBe(
    "/chat?panel=teams-issue-detail&detailId=iss_1",
  );
  expect(getByText("PAP-1")).toBeInTheDocument();
});

test("renders identifier label while linking by id", () => {
  const { container, getByText } = render(
    <IssueRow
      issue={{ id: "uuid-1", identifier: "ENG-42", title: "T", status: "todo" }}
    />,
  );
  const link = container.querySelector("a[data-inbox-issue-link]");
  expect(link?.getAttribute("href")).toBe(
    "/chat?panel=teams-issue-detail&detailId=uuid-1",
  );
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

test("inside the inline nav shell, clicking the row opens the detail in-shell", () => {
  // Under /chat an InlineTeamsNavProvider is mounted: the row must intercept
  // its own <Link> click, preventDefault, and route the issue's internal id
  // through openDetail("issue-detail", id) instead of a full Link navigation.
  const openDetail = vi.fn();
  const { container } = render(
    <InlineTeamsNavProvider value={{ openDetail, leaveToList: vi.fn() }}>
      <IssueRow issue={issue} />
    </InlineTeamsNavProvider>,
  );
  const link = container.querySelector(
    "a[data-inbox-issue-link]",
  ) as HTMLAnchorElement;
  expect(link).not.toBeNull();
  // The <Link href> stays present (middle-click / keyboard / new-tab all
  // still resolve to the canonical in-shell URL).
  expect(link.getAttribute("href")).toBe(
    "/chat?panel=teams-issue-detail&detailId=iss_1",
  );

  const clickEvent = new MouseEvent("click", {
    bubbles: true,
    cancelable: true,
  });
  link.dispatchEvent(clickEvent);

  expect(openDetail).toHaveBeenCalledTimes(1);
  expect(openDetail).toHaveBeenCalledWith("issue-detail", "iss_1");
  // Interception must cancel the native navigation.
  expect(clickEvent.defaultPrevented).toBe(true);
});

test("inside the shell, a modified click keeps native link behavior (new tab)", () => {
  // Cmd/Ctrl-click → new tab, Shift → new window, middle-click must NOT be
  // intercepted, or "open in new tab" on the detail href would be swallowed
  // and replace the inline panel instead.
  const openDetail = vi.fn();
  const { container } = render(
    <InlineTeamsNavProvider value={{ openDetail, leaveToList: vi.fn() }}>
      <IssueRow issue={issue} />
    </InlineTeamsNavProvider>,
  );
  const link = container.querySelector(
    "a[data-inbox-issue-link]",
  ) as HTMLAnchorElement;

  const cmdClick = new MouseEvent("click", {
    bubbles: true,
    cancelable: true,
    metaKey: true,
  });
  link.dispatchEvent(cmdClick);

  expect(openDetail).not.toHaveBeenCalled();
  expect(cmdClick.defaultPrevented).toBe(false);
});

test("without a nav provider the row click is not intercepted", () => {
  // No provider → no openDetail, no preventDefault, the <Link href> (the
  // canonical in-shell URL) drives navigation.
  const { container } = render(<IssueRow issue={issue} />);
  const link = container.querySelector(
    "a[data-inbox-issue-link]",
  ) as HTMLAnchorElement;
  expect(link.getAttribute("href")).toBe(
    "/chat?panel=teams-issue-detail&detailId=iss_1",
  );

  const clickEvent = new MouseEvent("click", {
    bubbles: true,
    cancelable: true,
  });
  link.dispatchEvent(clickEvent);

  // The handler is a no-op without a provider: it never calls preventDefault,
  // so the native <Link> navigation proceeds untouched.
  expect(clickEvent.defaultPrevented).toBe(false);
});
