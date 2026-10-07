import { describe, test, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// IssueRelations reads `GET /teams/issues` for blocker candidates.
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: vi.fn() }));

import { IssueRelations } from "@/components/teams/issues/IssueRelations";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { Issue } from "@/components/teams/shared/types";

const candidateA: Issue = { id: "iss_a", identifier: "PAP-2", title: "Alpha", status: "todo" };
const candidateB: Issue = { id: "iss_b", identifier: "PAP-3", title: "Bravo", status: "todo" };

function mockIssues(issues: Issue[]) {
  vi.mocked(useTeamsApi).mockReturnValue({
    read: vi.fn().mockReturnValue({ data: { issues } }),
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  } as unknown as ReturnType<typeof useTeamsApi>);
}

const baseIssue: Issue = {
  id: "iss_1",
  identifier: "PAP-1",
  title: "Current",
  status: "todo",
};

beforeEach(() => {
  vi.clearAllMocks();
  mockIssues([baseIssue, candidateA, candidateB]);
});

describe("IssueRelations", () => {
  test("renders editable Blocked by + read-only Blocking rows", () => {
    render(<IssueRelations issue={baseIssue} onBlockedByChange={vi.fn()} />);
    expect(screen.getByText("Blocked by")).toBeInTheDocument();
    expect(screen.getByText("Blocking")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /add blocker/i })).toBeInTheDocument();
  });

  test("the current issue is excluded from blocker candidates", async () => {
    const user = userEvent.setup();
    render(<IssueRelations issue={baseIssue} onBlockedByChange={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: /add blocker/i }));
    const list = await screen.findByRole("listbox");
    expect(within(list).queryByText(/Current/)).not.toBeInTheDocument();
    expect(within(list).getByText(/Alpha/)).toBeInTheDocument();
    expect(within(list).getByText(/Bravo/)).toBeInTheDocument();
  });

  test("adding a blocker sends the FULL desired id array (append)", async () => {
    const user = userEvent.setup();
    const onBlockedByChange = vi.fn();
    // Already blocked by A; adding B must send BOTH ids (replace semantics).
    const issue: Issue = {
      ...baseIssue,
      blockedBy: [{ id: "iss_a", identifier: "PAP-2", title: "Alpha", status: "todo" }],
    };
    render(<IssueRelations issue={issue} onBlockedByChange={onBlockedByChange} />);
    await user.click(screen.getByRole("button", { name: /add blocker/i }));
    await user.click(await screen.findByText(/Bravo/));
    expect(onBlockedByChange).toHaveBeenCalledWith(["iss_a", "iss_b"]);
  });

  test("toggling an already-selected blocker off removes it from the array", async () => {
    const user = userEvent.setup();
    const onBlockedByChange = vi.fn();
    const issue: Issue = {
      ...baseIssue,
      blockedBy: [
        { id: "iss_a", identifier: "PAP-2", title: "Alpha", status: "todo" },
        { id: "iss_b", identifier: "PAP-3", title: "Bravo", status: "todo" },
      ],
    };
    render(<IssueRelations issue={issue} onBlockedByChange={onBlockedByChange} />);
    await user.click(screen.getByRole("button", { name: /add blocker/i }));
    // Bravo is selected; clicking it again toggles it off.
    const list = await screen.findByRole("listbox");
    await user.click(within(list).getByText(/Bravo/));
    expect(onBlockedByChange).toHaveBeenCalledWith(["iss_a"]);
  });

  test("removing a chip confirms then sends the remaining ids", async () => {
    const user = userEvent.setup();
    const onBlockedByChange = vi.fn();
    const issue: Issue = {
      ...baseIssue,
      blockedBy: [
        { id: "iss_a", identifier: "PAP-2", title: "Alpha", status: "todo" },
        { id: "iss_b", identifier: "PAP-3", title: "Bravo", status: "todo" },
      ],
    };
    render(<IssueRelations issue={issue} onBlockedByChange={onBlockedByChange} />);
    // Click the chip's remove (X) for Alpha, then confirm in the dialog.
    await user.click(screen.getByRole("button", { name: /remove PAP-2 as blocker/i }));
    await user.click(await screen.findByRole("button", { name: /^remove blocker$/i }));
    expect(onBlockedByChange).toHaveBeenCalledWith(["iss_b"]);
  });

  test("renders Blocking chips read-only when issue.blocks is populated", () => {
    const issue: Issue = {
      ...baseIssue,
      blocks: [{ id: "iss_c", identifier: "PAP-9", title: "Downstream", status: "todo" }],
    };
    render(<IssueRelations issue={issue} onBlockedByChange={vi.fn()} />);
    expect(screen.getByRole("link", { name: /PAP-9/ })).toBeInTheDocument();
  });
});
