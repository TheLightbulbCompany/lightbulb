// apps/frontend/tests/unit/components/teams/inbox/InboxPage.test.tsx
//
// Atom-contract guard: locks in the editorial PanelMasthead at the top of the
// Inbox page. Behavior tests live in src/__tests__/components/teams/inbox/.

import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("@/components/teams/inbox/hooks/useInboxData", () => ({
  useInboxData: vi.fn(),
}));
vi.mock("@/components/teams/inbox/hooks/useInboxKeyboardNav", () => ({
  useInboxKeyboardNav: vi.fn(),
}));
vi.mock("@/components/teams/inbox/hooks/useInboxArchiveStack", () => ({
  useInboxArchiveStack: vi.fn(),
}));
vi.mock("@/components/teams/inbox/hooks/useReadInboxItems", () => ({
  useReadInboxItems: vi.fn(),
}));
vi.mock("@/components/teams/issues/hooks/useIssueMutations", () => ({
  useIssueMutations: vi.fn(),
}));

import { InboxPage } from "@/components/teams/inbox/InboxPage";
import { useInboxData } from "@/components/teams/inbox/hooks/useInboxData";
import { useInboxArchiveStack } from "@/components/teams/inbox/hooks/useInboxArchiveStack";
import { useReadInboxItems } from "@/components/teams/inbox/hooks/useReadInboxItems";
import { useIssueMutations } from "@/components/teams/issues/hooks/useIssueMutations";
import type { Issue } from "@/components/teams/shared/types";

beforeEach(() => {
  vi.mocked(useInboxData).mockReturnValue({
    mineIssues: [],
    touchedIssues: [],
    allIssues: [],
    isLoading: false,
    isError: false,
    error: null,
  });
  vi.mocked(useInboxArchiveStack).mockReturnValue({
    archivingIssueIds: new Set(),
    hasUndoableArchive: false,
    archive: vi.fn().mockResolvedValue(undefined),
    undoArchive: vi.fn().mockResolvedValue(undefined),
    markRead: vi.fn().mockResolvedValue(undefined),
    markUnread: vi.fn().mockResolvedValue(undefined),
  });
  vi.mocked(useReadInboxItems).mockReturnValue({
    readItemKeys: new Set(),
    isRead: () => false,
    markRead: vi.fn(),
    markUnread: vi.fn(),
    markManyRead: vi.fn(),
    clearAll: vi.fn(),
  });
  vi.mocked(useIssueMutations).mockReturnValue({
    create: vi.fn().mockResolvedValue({ id: "iss_new", title: "x" } as Issue),
    update: vi.fn().mockResolvedValue(undefined),
    remove: vi.fn().mockResolvedValue(undefined),
  } as unknown as ReturnType<typeof useIssueMutations>);
});

describe("InboxPage atom contract", () => {
  it("renders the editorial PanelMasthead with overline 'Inbox' and h1 title", () => {
    render(<InboxPage companyId="c1" currentUserId="u1" />);
    expect(screen.getByRole("banner")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 1, name: /inbox/i }),
    ).toBeInTheDocument();
  });
});
