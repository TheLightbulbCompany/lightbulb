// apps/frontend/tests/unit/components/teams/inbox/InboxToolbar.test.tsx

import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { InboxToolbar } from "@/components/teams/inbox/InboxToolbar";
import { defaultIssueFilterState } from "@/components/teams/shared/lib/issueFilters";

function makeProps(overrides: Partial<Parameters<typeof InboxToolbar>[0]> = {}) {
  return {
    tab: "mine" as const,
    onTabChange: vi.fn(),
    searchQuery: "",
    onSearchChange: vi.fn(),
    filterState: defaultIssueFilterState,
    onFilterChange: vi.fn(),
    agents: [],
    members: [],
    projects: [],
    labels: [],
    currentUserId: "user_test",
    unreadCount: 0,
    onMarkAllRead: vi.fn(),
    onNewIssue: vi.fn(),
    ...overrides,
  };
}

describe("InboxToolbar", () => {
  it("renders an editorial search input via PanelToolbar (role=searchbox)", () => {
    render(<InboxToolbar {...makeProps()} />);
    // Both mobile + desktop variants render — at least one searchbox should be present.
    const searchboxes = screen.getAllByRole("searchbox");
    expect(searchboxes.length).toBeGreaterThanOrEqual(1);
  });

  it("uses PanelToolbar's editorial input styling (cream bg, edge border)", () => {
    // This locks in the PanelToolbar adoption. PanelToolbar renders a raw
    // <input type="search"> with editorial-token classes instead of shadcn
    // Input's `border-input bg-background` defaults. If someone reverts to
    // shadcn Input or hand-rolled markup, this assertion catches it.
    render(<InboxToolbar {...makeProps()} />);
    const searchboxes = screen.getAllByRole("searchbox");
    expect(searchboxes.length).toBeGreaterThanOrEqual(1);
    for (const box of searchboxes) {
      expect(box.className).toMatch(/bg-\[var\(--cream\)\]/);
      expect(box.className).toMatch(/border-\[var\(--edge\)\]/);
    }
  });

  it("displays the controlled search query", () => {
    render(<InboxToolbar {...makeProps({ searchQuery: "release-notes" })} />);
    const searchboxes = screen.getAllByRole("searchbox");
    for (const box of searchboxes) {
      expect(box).toHaveValue("release-notes");
    }
  });

  it("calls onSearchChange when the user types in the search input", async () => {
    const onSearchChange = vi.fn();
    render(<InboxToolbar {...makeProps({ onSearchChange })} />);
    const searchboxes = screen.getAllByRole("searchbox");
    await userEvent.type(searchboxes[0], "x");
    expect(onSearchChange).toHaveBeenCalledWith("x");
  });

  it("renders the primary 'New issue' action via EditorialButton", () => {
    render(<InboxToolbar {...makeProps()} />);
    const btn = screen.getByRole("button", { name: /new issue/i });
    expect(btn).toBeInTheDocument();
    expect(btn).toHaveClass("editorial-btn", "editorial-btn--primary");
  });

  it("fires onNewIssue when the primary action is clicked", async () => {
    const onNewIssue = vi.fn();
    render(<InboxToolbar {...makeProps({ onNewIssue })} />);
    await userEvent.click(screen.getByRole("button", { name: /new issue/i }));
    expect(onNewIssue).toHaveBeenCalledTimes(1);
  });

  it("renders the 'Mark all as read' button as a secondary EditorialButton, disabled when unreadCount is 0", () => {
    render(<InboxToolbar {...makeProps({ unreadCount: 0 })} />);
    const btn = screen.getByRole("button", { name: /mark all as read/i });
    expect(btn).toBeInTheDocument();
    expect(btn).toHaveClass("editorial-btn", "editorial-btn--secondary");
    expect(btn).toBeDisabled();
  });

  it("enables 'Mark all as read' when there are unread items", () => {
    render(<InboxToolbar {...makeProps({ unreadCount: 5 })} />);
    expect(screen.getByRole("button", { name: /mark all as read/i })).not.toBeDisabled();
  });

  it("renders all four inbox tabs and reports the active one", () => {
    render(<InboxToolbar {...makeProps({ tab: "unread" })} />);
    const tabs = screen.getAllByRole("tab");
    expect(tabs).toHaveLength(4);
    const unread = screen.getByRole("tab", { name: "Unread" });
    expect(unread).toHaveAttribute("aria-selected", "true");
  });

  it("calls onTabChange when a tab is clicked", async () => {
    const onTabChange = vi.fn();
    render(<InboxToolbar {...makeProps({ onTabChange })} />);
    await userEvent.click(screen.getByRole("tab", { name: "Recent" }));
    expect(onTabChange).toHaveBeenCalledWith("recent");
  });
});
