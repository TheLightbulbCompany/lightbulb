// @vitest-environment jsdom
// The Work page: every task-board page is a tab of one dock tab, "Work".
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

const openPanel = vi.fn();
vi.mock("../workspaceController", () => ({ openPanel: (...a: unknown[]) => openPanel(...a) }));

import { WorkTabs, WORK_TEAMS_KEYS } from "../panels/WorkTabs";

describe("WorkTabs", () => {
  beforeEach(() => openPanel.mockClear());

  it("lists every Work page in order and marks the open one", () => {
    render(<WorkTabs teamsKey="projects" />);
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((t) => t.textContent)).toEqual(["Tasks", "Projects", "Goals", "Automations", "Cases", "Agents", "Money"]);
    expect(screen.getByRole("tab", { name: "Projects" })).toHaveAttribute("aria-selected", "true");
  });

  it("switches page inside the same Work tab", () => {
    render(<WorkTabs teamsKey="issues" />);
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Goals" }), { button: 0 });
    expect(openPanel).toHaveBeenCalledWith("teams-goals", {}, { pushUrl: true });
  });

  it("covers exactly the pages the Work tab hosts", () => {
    expect([...WORK_TEAMS_KEYS]).toEqual(["issues", "projects", "goals", "routines", "cases", "agents", "money"]);
  });
});
