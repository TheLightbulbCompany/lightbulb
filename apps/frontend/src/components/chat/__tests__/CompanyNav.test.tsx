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
  "Payments",
  "Products",
  "Customers",
  "Spend",
  "Tasks",
  "Automations",
  "Goals",
  "Projects",
  "Brain",
  "Library",
  "Org chart",
  "Accounts",
  "Activity",
];

describe("CompanyNav", () => {
  it("renders every company panel", () => {
    render(<CompanyNav />);
    for (const label of ALL_LABELS) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });
  it("declares the spec §5.1 section order: top block, Money, Work, Knowledge, Company", () => {
    expect(COMPANY_SECTIONS.map((s) => s.label)).toEqual([
      null,
      "Money",
      "Work",
      "Knowledge",
      "Company",
    ]);
  });
  it("files the four Money tabs in spec order and opens teams-spend on click", () => {
    // The three Stripe reads first, then what the agents cost. Products is a
    // peer tab, not a section of Payments — all three read one connected
    // account and none is subordinate to another.
    const money = COMPANY_SECTIONS.find((s) => s.label === "Money");
    expect(money?.items.map((i) => i.label)).toEqual([
      "Payments",
      "Products",
      "Customers",
      "Spend",
    ]);
    const onPanelChange = vi.fn();
    render(<CompanyNav onPanelChange={onPanelChange} />);
    fireEvent.click(screen.getByText("Spend"));
    expect(onPanelChange).toHaveBeenCalledWith("teams-spend");
  });
  it('puts "Home" first', () => {
    expect(COMPANY_SECTIONS[0].items[0].label).toBe("Home");
  });
  it('files "Brain" and "Library" under Knowledge and opens teams-artifacts on click', () => {
    const knowledge = COMPANY_SECTIONS.find((s) => s.label === "Knowledge");
    expect(knowledge?.items.map((i) => i.label)).toEqual(["Brain", "Library"]);
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
    fireEvent.click(screen.getByText("Brain"));
    expect(onPanelChange).toHaveBeenCalledWith("teams-brain");
  });
  it("marks the active item with the midnight accent", () => {
    render(<CompanyNav activePanel="teams-routines" />);
    const btn = screen.getByText("Automations").closest("button");
    expect(btn?.className).toMatch(/--midnight/);
  });
  // The desktop shell paints this nav while the provisioning stepper has the
  // dockview host unmounted, and `openPanel` early-returns when the dock api is
  // null — so every one of these clicks was a silent no-op with the button
  // rendered fully enabled. An inert-but-enabled control is the same class of
  // defect as a false progress bar: the UI asserting a capability it lacks.
  it("renders items as unavailable, with a reason, when disabled", () => {
    const onPanelChange = vi.fn();
    render(
      <CompanyNav
        disabled
        disabledTitle="Available once your workspace is ready"
        onPanelChange={onPanelChange}
      />,
    );
    for (const label of ALL_LABELS) {
      const btn = screen.getByText(label).closest("button");
      expect(btn, `"${label}" should be disabled`).toBeDisabled();
      expect(btn).toHaveAttribute("title", "Available once your workspace is ready");
    }
    fireEvent.click(screen.getByText("Tasks"));
    expect(onPanelChange).not.toHaveBeenCalled();
  });

  it("is enabled and untitled by default (mobile's ChatLayout routes it somewhere live)", () => {
    render(<CompanyNav onPanelChange={vi.fn()} />);
    const btn = screen.getByText("Tasks").closest("button");
    expect(btn).toBeEnabled();
    expect(btn).not.toHaveAttribute("title");
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
