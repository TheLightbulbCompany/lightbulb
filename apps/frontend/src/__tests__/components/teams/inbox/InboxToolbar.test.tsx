import { describe, test, expect, vi, beforeEach } from "vitest";
import { render, fireEvent, screen } from "@testing-library/react";
import { InboxToolbar } from "@/components/teams/inbox/InboxToolbar";
import { defaultIssueFilterState } from "@/components/teams/shared/lib/issueFilters";

const baseProps = {
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
  currentUserId: "u1",
  unreadCount: 0,
  onMarkAllRead: vi.fn(),
  onNewIssue: vi.fn(),
};

beforeEach(() => {
  vi.clearAllMocks();
});

describe("InboxToolbar", () => {
  test("renders all 4 tabs", () => {
    render(<InboxToolbar {...baseProps} />);
    expect(screen.getByRole("tab", { name: /mine/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /recent/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /unread/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /^all$/i })).toBeInTheDocument();
  });

  test("clicking a tab fires onTabChange with the right value", () => {
    const onTabChange = vi.fn();
    render(<InboxToolbar {...baseProps} onTabChange={onTabChange} />);
    fireEvent.click(screen.getByRole("tab", { name: /unread/i }));
    expect(onTabChange).toHaveBeenCalledWith("unread");
  });

  test("active tab gets the active styling", () => {
    // Editorial retheme: active tab uses inline `style={{background:
    // var(--cream-deep), color: var(--midnight)}}` rather than amber Tailwind
    // classes. Assert via the inline style attribute and aria-selected.
    const { rerender } = render(<InboxToolbar {...baseProps} tab="mine" />);
    const mineBtn = screen.getByRole("tab", { name: /mine/i });
    expect(mineBtn).toHaveAttribute("aria-selected", "true");
    expect(mineBtn.getAttribute("style") ?? "").toMatch(/--cream-deep/);

    rerender(<InboxToolbar {...baseProps} tab="recent" />);
    const recentBtn = screen.getByRole("tab", { name: /recent/i });
    expect(recentBtn).toHaveAttribute("aria-selected", "true");
    expect(recentBtn.getAttribute("style") ?? "").toMatch(/--cream-deep/);
  });

  test("typing in search fires onSearchChange (each keystroke)", () => {
    const onSearchChange = vi.fn();
    render(<InboxToolbar {...baseProps} onSearchChange={onSearchChange} />);
    // Both mobile + desktop variants render via PanelToolbar, which emits
    // `<input type="search">` (role=searchbox).
    const inputs = screen.getAllByRole("searchbox");
    expect(inputs.length).toBe(2);
    fireEvent.change(inputs[0], { target: { value: "fix" } });
    expect(onSearchChange).toHaveBeenCalledWith("fix");
  });

  test("Mark all as read button is disabled when unreadCount=0", () => {
    render(<InboxToolbar {...baseProps} unreadCount={0} />);
    const btn = screen.getByRole("button", { name: /mark all as read/i });
    expect(btn).toBeDisabled();
  });

  test("Mark all as read button enabled when unreadCount>0", () => {
    render(<InboxToolbar {...baseProps} unreadCount={5} />);
    const btn = screen.getByRole("button", { name: /mark all as read/i });
    expect(btn).not.toBeDisabled();
  });

  test("Mark all confirmation dialog fires onMarkAllRead on confirm", async () => {
    const onMarkAllRead = vi.fn();
    render(
      <InboxToolbar
        {...baseProps}
        unreadCount={3}
        onMarkAllRead={onMarkAllRead}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /mark all as read/i }));
    const confirm = await screen.findByRole("button", { name: /confirm/i });
    fireEvent.click(confirm);
    expect(onMarkAllRead).toHaveBeenCalled();
  });

  test("Mark all dialog text shows the count", async () => {
    render(<InboxToolbar {...baseProps} unreadCount={5} />);
    fireEvent.click(screen.getByRole("button", { name: /mark all as read/i }));
    expect(await screen.findByText(/5 unread items/i)).toBeInTheDocument();
  });

  test("Mark all dialog uses singular 'item' for unreadCount=1", async () => {
    render(<InboxToolbar {...baseProps} unreadCount={1} />);
    fireEvent.click(screen.getByRole("button", { name: /mark all as read/i }));
    expect(await screen.findByText(/1 unread item\b/i)).toBeInTheDocument();
  });

  test("IssueFiltersPopover trigger renders", () => {
    render(<InboxToolbar {...baseProps} />);
    // Filters button should be present (iconOnly variant uses aria-label/title 'Filter').
    expect(screen.getByRole("button", { name: /filter/i })).toBeInTheDocument();
  });

  test("renders the primary 'New task' button", () => {
    render(<InboxToolbar {...baseProps} />);
    expect(
      screen.getByRole("button", { name: /new task/i }),
    ).toBeInTheDocument();
  });

  test("clicking 'New task' fires onNewIssue", () => {
    const onNewIssue = vi.fn();
    render(<InboxToolbar {...baseProps} onNewIssue={onNewIssue} />);
    fireEvent.click(screen.getByRole("button", { name: /new task/i }));
    expect(onNewIssue).toHaveBeenCalledTimes(1);
  });

  test("filter trigger reflects active filter count badge", () => {
    render(
      <InboxToolbar
        {...baseProps}
        filterState={{
          ...defaultIssueFilterState,
          statuses: ["todo"],
          priorities: ["high"],
        }}
      />,
    );
    // Trigger is iconOnly with title="Filters: 2" when activeFilterCount=2.
    // Verify the toolbar's countActiveFilters helper produced 2.
    const trigger = document.querySelector('button[title="Filters: 2"]');
    expect(trigger).not.toBeNull();
  });

  test("leads with a 'Needs you' tab", () => {
    render(<InboxToolbar {...baseProps} />);
    const tabs = screen.getAllByRole("tab").map((t) => t.textContent ?? "");
    // First, because a decision nobody can find is a decision nobody makes.
    expect(tabs[0]).toMatch(/needs you/i);
  });

  test("badges the count of decisions waiting", () => {
    render(<InboxToolbar {...baseProps} attentionCount={9} />);
    const tab = screen.getByRole("tab", { name: /needs you/i });
    expect(tab).toHaveTextContent("9");
  });

  test("renders no badge when nothing is waiting", () => {
    // A hopeful zero is a fabricated count. Absent means absent.
    render(<InboxToolbar {...baseProps} attentionCount={0} />);
    expect(screen.getByRole("tab", { name: /needs you/i })).not.toHaveTextContent("0");
  });

  test("the tab bar is rendered once, so mobile can reach it too", () => {
    // It used to live inside the desktop-only row; the one surface listing
    // pending approvals would have been unreachable on a phone.
    render(<InboxToolbar {...baseProps} />);
    expect(screen.getAllByRole("tablist")).toHaveLength(1);
  });
});
