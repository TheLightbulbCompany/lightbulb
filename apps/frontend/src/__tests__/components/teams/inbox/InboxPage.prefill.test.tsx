import { describe, test, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import type { CompanyAgent, Issue } from "@/components/teams/shared/types";

// Mutable holder so a test can flip the search params mid-lifecycle (the real
// page's effect strips `?newIssue` via router.replace after the dialog opens).
const params = vi.hoisted(() => ({
  current: new URLSearchParams("newIssue=onboarding"),
}));

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
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: () => ({
      data: undefined,
      isLoading: false,
      error: undefined,
      isValidating: false,
      mutate: vi.fn(),
    }),
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  }),
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: vi.fn(),
    replace: vi.fn(),
    prefetch: vi.fn(),
    back: vi.fn(),
  }),
  usePathname: () => "/",
  useSearchParams: () => params.current,
}));

import { InboxPage } from "@/components/teams/inbox/InboxPage";
import { useInboxData } from "@/components/teams/inbox/hooks/useInboxData";
import { useInboxKeyboardNav } from "@/components/teams/inbox/hooks/useInboxKeyboardNav";
import { useInboxArchiveStack } from "@/components/teams/inbox/hooks/useInboxArchiveStack";
import { useReadInboxItems } from "@/components/teams/inbox/hooks/useReadInboxItems";
import { useIssueMutations } from "@/components/teams/issues/hooks/useIssueMutations";

const agents: CompanyAgent[] = [
  { id: "friday", name: "Friday", role: "ceo", seeded: true },
];

beforeEach(() => {
  vi.clearAllMocks();
  params.current = new URLSearchParams("newIssue=onboarding");
  vi.mocked(useInboxData).mockReturnValue({
    mineIssues: [] as Issue[],
    touchedIssues: [] as Issue[],
    allIssues: [] as Issue[],
    isLoading: false,
    isError: false,
  } as unknown as ReturnType<typeof useInboxData>);
  vi.mocked(useInboxKeyboardNav).mockReturnValue(
    undefined as unknown as ReturnType<typeof useInboxKeyboardNav>,
  );
  vi.mocked(useInboxArchiveStack).mockReturnValue({
    archivingIssueIds: new Set<string>(),
    hasUndoableArchive: false,
    archive: vi.fn().mockResolvedValue(undefined),
    undoArchive: vi.fn().mockResolvedValue(undefined),
    markRead: vi.fn().mockResolvedValue(undefined),
    markUnread: vi.fn().mockResolvedValue(undefined),
  } as unknown as ReturnType<typeof useInboxArchiveStack>);
  vi.mocked(useReadInboxItems).mockReturnValue({
    readItemKeys: new Set(),
    isRead: () => false,
    markRead: vi.fn(),
    markUnread: vi.fn(),
    markManyRead: vi.fn(),
    clearAll: vi.fn(),
  } as unknown as ReturnType<typeof useReadInboxItems>);
  vi.mocked(useIssueMutations).mockReturnValue({
    create: vi.fn().mockResolvedValue({ id: "iss_new" }),
    createChild: vi.fn(),
    update: vi.fn(),
    addComment: vi.fn(),
    remove: vi.fn(),
    monitorCheckNow: vi.fn(),
  } as unknown as ReturnType<typeof useIssueMutations>);
});

describe("InboxPage onboarding pre-fill", () => {
  test("?newIssue=onboarding opens the dialog pre-filled with the CEO task", () => {
    render(
      <InboxPage companyId="co_1" currentUserId="u_1" agents={agents} />,
    );
    expect(screen.getByPlaceholderText("Task title")).toHaveValue(
      "Build out my team for [your goal]",
    );
    expect(screen.getByLabelText(/assign to agent/i)).toHaveValue("friday");
  });

  test("stays open + pre-filled after the param is stripped", () => {
    const { rerender } = render(
      <InboxPage companyId="co_1" currentUserId="u_1" agents={agents} />,
    );
    expect(screen.getByPlaceholderText("Task title")).toHaveValue(
      "Build out my team for [your goal]",
    );

    // Simulate the effect that strips `?newIssue` after the dialog opens: the
    // param vanishes, but the prefill lives in React state so it must survive.
    params.current = new URLSearchParams("");
    rerender(
      <InboxPage companyId="co_1" currentUserId="u_1" agents={agents} />,
    );
    expect(screen.getByPlaceholderText("Task title")).toHaveValue(
      "Build out my team for [your goal]",
    );
  });
});
