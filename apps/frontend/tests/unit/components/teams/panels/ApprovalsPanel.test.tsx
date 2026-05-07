// apps/frontend/tests/unit/components/teams/panels/ApprovalsPanel.test.tsx
//
// Atom-contract guard: locks in PanelMasthead + PanelEmptyState +
// EditorialButton (primary Approve, destructive Reject) composition on the
// Approvals panel. See plan 2026-05-07-teams-dashboard-approvals-atoms-port.md.

import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

const mockRead = vi.fn();
const mockPost = vi.fn();

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: mockRead,
    post: mockPost,
    patch: vi.fn(),
    del: vi.fn(),
  }),
}));

import { ApprovalsPanel } from "@/components/teams/panels/ApprovalsPanel";

describe("ApprovalsPanel atom contract", () => {
  beforeEach(() => {
    mockRead.mockReset();
    mockPost.mockReset();
  });

  it("renders PanelMasthead with overline + h1 title", () => {
    mockRead.mockReturnValue({
      data: { approvals: [] },
      mutate: vi.fn(),
    });
    render(<ApprovalsPanel />);
    expect(screen.getByRole("banner")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 1, name: /approvals/i }),
    ).toBeInTheDocument();
  });

  it("renders PanelEmptyState when there are no approvals", () => {
    mockRead.mockReturnValue({
      data: { approvals: [] },
      mutate: vi.fn(),
    });
    render(<ApprovalsPanel />);
    const headline = screen.getByRole("heading", { level: 2 });
    expect(headline).toHaveClass("font-editorial-serif");
    expect(headline).toHaveTextContent(/no pending approvals/i);
    expect(
      screen.getByText(/when an agent action requires sign-off/i),
    ).toBeInTheDocument();
  });

  it("renders approve + reject EditorialButtons for each approval", () => {
    mockRead.mockReturnValue({
      data: {
        approvals: [
          {
            id: "a1",
            title: "Test approval",
            description: "Body",
          },
        ],
      },
      mutate: vi.fn(),
    });
    render(<ApprovalsPanel />);
    expect(screen.getByText("Test approval")).toBeInTheDocument();

    const approve = screen.getByRole("button", { name: /approve/i });
    expect(approve).toBeInTheDocument();
    // EditorialButton emits the editorial-btn base + variant + size classes.
    expect(approve).toHaveClass("editorial-btn");
    expect(approve).toHaveClass("editorial-btn--primary");
    expect(approve).toHaveClass("editorial-btn--sm");

    const reject = screen.getByRole("button", { name: /reject/i });
    expect(reject).toBeInTheDocument();
    expect(reject).toHaveClass("editorial-btn");
    expect(reject).toHaveClass("editorial-btn--destructive");
    expect(reject).toHaveClass("editorial-btn--sm");
  });
});
