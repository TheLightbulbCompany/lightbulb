import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { CompanyNav, COMPANY_SECTIONS } from "../CompanyNav";
import { resolvePanelAlias, describePanel } from "../../workspace/panelRegistry";

// The live nav set, in the spec §5.1 IA. "Issues" was renamed to "Tasks" and
// Members removed (#1022), Costs cut (#1003), Skills retired for in-app
// ClawHub search (#1175); Artifacts (#1307, now "Library") and Search (#1312)
// were added; "Dashboard" became "Home" with the IA shell.
const ALL_LABELS = [
  "Home",
  "Search",
  "Inbox",
  "Tasks",
  "Automations",
  "Goals",
  "Projects",
  "Library",
  "Org chart",
  "Activity",
];

describe("CompanyNav", () => {
  it("renders every company panel", () => {
    render(<CompanyNav />);
    for (const label of ALL_LABELS) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });
  it("declares the spec §5.1 section order: top block, Work, Knowledge, Company", () => {
    expect(COMPANY_SECTIONS.map((s) => s.label)).toEqual([null, "Work", "Knowledge", "Company"]);
  });
  it('puts "Home" first', () => {
    expect(COMPANY_SECTIONS[0].items[0].label).toBe("Home");
  });
  it('files "Library" under Knowledge and opens teams-artifacts on click', () => {
    const knowledge = COMPANY_SECTIONS.find((s) => s.label === "Knowledge");
    expect(knowledge?.items.map((i) => i.label)).toEqual(["Library"]);
    const onPanelChange = vi.fn();
    render(<CompanyNav onPanelChange={onPanelChange} />);
    fireEvent.click(screen.getByText("Library"));
    expect(onPanelChange).toHaveBeenCalledWith("teams-artifacts");
  });
  it("has no dead ends — every item's panel key resolves in the registry", () => {
    for (const section of COMPANY_SECTIONS) {
      for (const item of section.items) {
        expect(
          describePanel(resolvePanelAlias(item.panel), {}),
          `panel key "${item.panel}" does not resolve`,
        ).not.toBeNull();
      }
    }
  });
  it("emits the teams- panel key on click", () => {
    const onPanelChange = vi.fn();
    render(<CompanyNav onPanelChange={onPanelChange} />);
    // Label is "Tasks"; the panel key stayed `teams-issues`.
    fireEvent.click(screen.getByText("Tasks"));
    expect(onPanelChange).toHaveBeenCalledWith("teams-issues");
  });
  it("marks the active item with the midnight accent", () => {
    render(<CompanyNav activePanel="teams-routines" />);
    const btn = screen.getByText("Automations").closest("button");
    expect(btn?.className).toMatch(/--midnight/);
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
});
