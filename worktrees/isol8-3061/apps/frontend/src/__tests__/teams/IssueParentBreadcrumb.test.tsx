// apps/frontend/src/__tests__/teams/IssueParentBreadcrumb.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

const read = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read }) }));

import { IssueParentBreadcrumb } from "@/components/teams/issues/IssueParentBreadcrumb";

describe("IssueParentBreadcrumb", () => {
  it("links to the parent issue with its title", () => {
    read.mockReturnValue({ data: { id: "p1", title: "Ship billing" }, isLoading: false, error: null, mutate: vi.fn() });
    render(<IssueParentBreadcrumb parentId="p1" />);
    const link = screen.getByRole("link", { name: /Ship billing/ });
    expect(link).toHaveAttribute(
      "href",
      "/chat?panel=teams-issue-detail&detailId=p1",
    );
  });

  it("falls back to 'Parent issue' before the title loads", () => {
    read.mockReturnValue({ data: undefined, isLoading: true, error: null, mutate: vi.fn() });
    render(<IssueParentBreadcrumb parentId="p1" />);
    expect(screen.getByRole("link", { name: /parent issue/i })).toHaveAttribute(
      "href",
      "/chat?panel=teams-issue-detail&detailId=p1",
    );
  });
});
