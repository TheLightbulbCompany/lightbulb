// apps/frontend/tests/unit/components/teams/panels/MembersPanel.test.tsx
//
// Atom-contract guard: locks in the editorial PanelMasthead + PanelEmptyState +
// PanelDataTable composition on the Members panel, and the Clerk-dashboard
// deeplink CTA. Member management lives in Clerk (org membership is the
// source of truth) — there is intentionally no in-app invite/role-change
// surface here.

import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

const mockRead = vi.fn();

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: mockRead,
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  }),
}));

import { MembersPanel } from "@/components/teams/panels/MembersPanel";

describe("MembersPanel atom contract", () => {
  beforeEach(() => {
    mockRead.mockReset();
  });

  it("renders the editorial PanelMasthead with overline + h1 title", () => {
    mockRead.mockReturnValue({
      data: { members: [] },
      isLoading: false,
      error: null,
    });

    render(<MembersPanel />);
    expect(screen.getByRole("banner")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 1, name: /members/i }),
    ).toBeInTheDocument();
  });

  it("renders the Clerk-dashboard deeplink CTA in the masthead actions", () => {
    mockRead.mockReturnValue({
      data: { members: [] },
      isLoading: false,
      error: null,
    });

    render(<MembersPanel />);
    const link = screen.getByRole("link", { name: /manage in clerk/i });
    expect(link).toBeInTheDocument();
    expect(link).toHaveAttribute("href", "https://dashboard.clerk.com");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("renders PanelEmptyState shape (h2 with editorial Lora-italic class) when members list is empty", () => {
    mockRead.mockReturnValue({
      data: { members: [] },
      isLoading: false,
      error: null,
    });

    render(<MembersPanel />);
    const headline = screen.getByRole("heading", { level: 2 });
    expect(headline).toHaveClass("font-editorial-serif");
    expect(headline).toHaveTextContent(/no members yet/i);
  });

  it("renders a Loading… status while SWR is fetching (Codex P2 regression)", () => {
    // Initial fetch: data is undefined + isLoading is true. Without the
    // explicit loading branch the panel would fall through to PanelDataTable
    // with zero rows and look like the list is empty.
    mockRead.mockReturnValue({
      data: undefined,
      isLoading: true,
      error: null,
    });

    render(<MembersPanel />);
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent(/loading/i);
    // The empty-state h2 must not render in the loading branch — that would
    // mislead users into thinking they have no members yet.
    expect(screen.queryByRole("heading", { level: 2 })).not.toBeInTheDocument();
  });

  it("Clerk CTA is a single interactive control (not a nested <a><button>)", () => {
    // Codex P2 regression: nested interactive controls are invalid HTML and
    // break keyboard / screen-reader activation. The deeplink must render as
    // a single <a> styled with editorial-btn classes — no <button> child.
    mockRead.mockReturnValue({
      data: { members: [] },
      isLoading: false,
      error: null,
    });

    render(<MembersPanel />);
    const link = screen.getByRole("link", { name: /manage in clerk/i });
    expect(link.tagName).toBe("A");
    expect(link).toHaveClass("editorial-btn");
    // Must NOT contain a nested <button>.
    expect(link.querySelector("button")).toBeNull();
  });

  it("renders PanelDataTable rows when members are present", () => {
    mockRead.mockReturnValue({
      data: {
        members: [
          {
            id: "m1",
            user_id: "user_1",
            email_via_clerk: "alice@example.com",
            role: "admin",
            joined_at: "2026-04-01",
          },
          {
            id: "m2",
            user_id: "user_2",
            email_via_clerk: "bob@example.com",
            role: "member",
            joined_at: "2026-04-15",
          },
        ],
      },
      isLoading: false,
      error: null,
    });

    render(<MembersPanel />);
    expect(screen.getByText("alice@example.com")).toBeInTheDocument();
    expect(screen.getByText("bob@example.com")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 2 })).not.toBeInTheDocument();
  });
});
