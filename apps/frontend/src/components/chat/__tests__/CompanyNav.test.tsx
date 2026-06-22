import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { CompanyNav } from "../CompanyNav";

const ALL_LABELS = [
  "Dashboard",
  "Inbox",
  "Issues",
  "Routines",
  "Goals",
  "Projects",
  "Org chart",
  "Skills",
  "Costs",
  "Activity",
  "Members",
];

describe("CompanyNav", () => {
  it("renders every company panel", () => {
    render(<CompanyNav />);
    for (const label of ALL_LABELS) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });
  it("groups items under the Work and Company section labels", () => {
    render(<CompanyNav />);
    expect(screen.getByText("Work")).toBeInTheDocument();
    expect(screen.getByText("Company")).toBeInTheDocument();
  });
  it("emits the teams- panel key on click", () => {
    const onPanelChange = vi.fn();
    render(<CompanyNav onPanelChange={onPanelChange} />);
    fireEvent.click(screen.getByText("Issues"));
    expect(onPanelChange).toHaveBeenCalledWith("teams-issues");
  });
  it("marks the active item with the forest accent", () => {
    render(<CompanyNav activePanel="teams-routines" />);
    const btn = screen.getByText("Routines").closest("button");
    expect(btn?.className).toMatch(/forest/);
  });
  it("is a shrinkable scroll region so the expanded nav can't clip the sidebar", () => {
    render(<CompanyNav />);
    const nav = screen.getByRole("navigation", { name: "Company" });
    expect(nav.className).toMatch(/min-h-0/);
    expect(nav.className).toMatch(/overflow-y-auto/);
  });
});
