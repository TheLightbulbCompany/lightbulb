// apps/frontend/tests/unit/components/teams/issues/NewIssueDialog.test.tsx
//
// Atom-contract guard: locks in EditorialDialog (role=dialog) shell + the
// Cancel/Create EditorialButton pair on the New-issue dialog. Behavior tests
// (submit body shape, defaults, project/agent selects) live in
// src/__tests__/components/teams/issues/NewIssueDialog.test.tsx.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("@/components/teams/issues/hooks/useIssueMutations", () => ({
  useIssueMutations: vi.fn(),
}));

import { NewIssueDialog } from "@/components/teams/issues/NewIssueDialog";
import { useIssueMutations } from "@/components/teams/issues/hooks/useIssueMutations";

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(useIssueMutations).mockReturnValue({
    create: vi.fn(),
    update: vi.fn(),
    addComment: vi.fn(),
  });
});

describe("NewIssueDialog atom contract", () => {
  it("renders inside EditorialDialog (role=dialog)", () => {
    render(<NewIssueDialog open onOpenChange={() => {}} />);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("renders the editorial dialog title 'New issue'", () => {
    render(<NewIssueDialog open onOpenChange={() => {}} />);
    expect(
      screen.getByRole("heading", { name: /new issue/i }),
    ).toBeInTheDocument();
  });

  it("renders Cancel and Create-issue actions as EditorialButtons (role=button)", () => {
    render(<NewIssueDialog open onOpenChange={() => {}} />);
    expect(screen.getByRole("button", { name: /create issue/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^cancel$/i })).toBeInTheDocument();
  });
});
