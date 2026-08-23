import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { CompanyNav } from "../CompanyNav";

// The live nav set. "Issues" was renamed to "Tasks" and Members removed
// (#1022), Costs cut (#1003), Skills retired for in-app ClawHub search
// (#1175); Artifacts (#1307) and Search (#1312) were added.
const ALL_LABELS = [
  "Dashboard",
  "Search",
  "Inbox",
  "Tasks",
  "Automations",
  "Goals",
  "Projects",
  "Org chart",
  "Artifacts",
  "Activity",
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
    // Label is "Tasks"; the panel key stayed `teams-issues`.
    fireEvent.click(screen.getByText("Tasks"));
    expect(onPanelChange).toHaveBeenCalledWith("teams-issues");
  });
  it("marks the active item with the forest accent", () => {
    render(<CompanyNav activePanel="teams-routines" />);
    const btn = screen.getByText("Automations").closest("button");
    expect(btn?.className).toMatch(/forest/);
  });
  it("owns no scroll region — the shared .sidebar-scroll wrapper owns the single scroll", () => {
    // Inverted deliberately in #1002: the nav used to carry its own
    // `min-h-0 overflow-y-auto`, which nested a second scroll inside
    // `.sidebar-scroll` and collapsed the roster's height. Re-adding any
    // overflow/scroll class here reintroduces that bug.
    render(<CompanyNav />);
    const nav = screen.getByRole("navigation", { name: "Company" });
    expect(nav.className).not.toMatch(/overflow-/);
    expect(nav.className).not.toMatch(/\boverscroll-/);
  });
  it("carries Connectors, whose key is a control panel rather than a teams one", () => {
    // The only non-teams entry in the nav. It lives here because a connector
    // is something the roster USES: a broken one reads as a blocked agent, so
    // burying it in Settings put it two surfaces away from what it breaks.
    const onPanelChange = vi.fn();
    render(<CompanyNav onPanelChange={onPanelChange} />);
    fireEvent.click(screen.getByText("Connectors"));
    expect(onPanelChange).toHaveBeenCalledWith("integrations");
  });

  it("highlights Connectors from the bare control key, not a prefixed panel id", () => {
    // panelRegistry ids control panels `control:<key>`; the caller strips that
    // prefix before handing it here. If the two ever disagree the item simply
    // never lights up, which is silent — so pin the shape both sides agree on.
    render(<CompanyNav activePanel="integrations" />);
    const btn = screen.getByText("Connectors").closest("button");
    expect(btn?.className).toMatch(/forest/);
  });
});
