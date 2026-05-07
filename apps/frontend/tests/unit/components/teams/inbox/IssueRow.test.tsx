// apps/frontend/tests/unit/components/teams/inbox/IssueRow.test.tsx
//
// Atom-contract guard: locks in EditorialButton usage for IssueRow's icon-only
// trailing action buttons (mark-as-read + dismiss-from-inbox). Behavior tests
// live in src/__tests__/components/teams/inbox/IssueRow.test.tsx.

import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { IssueRow } from "@/components/teams/inbox/IssueRow";
import type { Issue } from "@/components/teams/shared/types";

const issue: Issue = {
  id: "iss_atom_1",
  identifier: "PAP-77",
  title: "Atom-contract row",
  status: "todo",
  unread: true,
};

describe("IssueRow atom contract", () => {
  it("mark-as-read button has accessible name and uses EditorialButton", () => {
    render(
      <IssueRow
        issue={issue}
        unreadState="visible"
        onMarkRead={vi.fn()}
      />,
    );
    const markRead = screen.getByRole("button", { name: /mark as read/i });
    expect(markRead).toBeInTheDocument();
    expect(markRead.classList.contains("editorial-btn")).toBe(true);
  });

  it("dismiss-from-inbox (archive) button has accessible name and uses EditorialButton", () => {
    render(
      <IssueRow
        issue={issue}
        unreadState="hidden"
        onArchive={vi.fn()}
      />,
    );
    const archive = screen.getByRole("button", {
      name: /dismiss from inbox/i,
    });
    expect(archive).toBeInTheDocument();
    expect(archive.classList.contains("editorial-btn")).toBe(true);
  });
});
