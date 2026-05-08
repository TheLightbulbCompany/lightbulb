import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PanelTabs } from "@/components/teams/shell/PanelTabs";

const TABS = [
  { value: "default", label: "Default", count: 3 },
  { value: "unread", label: "Unread", count: 1 },
  { value: "archived", label: "Archived" },
];

describe("PanelTabs", () => {
  it("renders all tabs with counts when present", () => {
    render(<PanelTabs tabs={TABS} value="default" onValueChange={() => {}} />);
    expect(screen.getByRole("tab", { name: /default/i })).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("1")).toBeInTheDocument();
    expect(screen.queryByText("0")).not.toBeInTheDocument();
  });

  it("marks the active tab", () => {
    render(<PanelTabs tabs={TABS} value="unread" onValueChange={() => {}} />);
    expect(screen.getByRole("tab", { name: /unread/i })).toHaveAttribute("data-state", "active");
  });

  it("calls onValueChange when a tab is clicked", async () => {
    const onValueChange = vi.fn();
    render(<PanelTabs tabs={TABS} value="default" onValueChange={onValueChange} />);
    await userEvent.click(screen.getByRole("tab", { name: /archived/i }));
    expect(onValueChange).toHaveBeenCalledWith("archived");
  });

  it("does not render badge when count is zero", () => {
    const tabsWithZero = [
      { value: "a", label: "A", count: 0 },
      { value: "b", label: "B", count: 5 },
    ];
    render(<PanelTabs tabs={tabsWithZero} value="a" onValueChange={() => {}} />);
    expect(screen.queryByText("0")).not.toBeInTheDocument();
    expect(screen.getByText("5")).toBeInTheDocument();
  });

  it("merges caller className", () => {
    render(<PanelTabs tabs={TABS} value="default" onValueChange={() => {}} className="custom" />);
    // Tabs root rendered as a div by shadcn
    const tabsRoot = screen.getByRole("tab", { name: /default/i }).closest('[class*="custom"]');
    expect(tabsRoot).not.toBeNull();
  });
});
