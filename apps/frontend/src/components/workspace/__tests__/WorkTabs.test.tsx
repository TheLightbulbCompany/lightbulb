// @vitest-environment jsdom
// The Work page: every task-board page is a tab of one dock tab, "Work".
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

const openPanel = vi.fn();
vi.mock("../workspaceController", () => ({ openPanel: (...a: unknown[]) => openPanel(...a) }));
const live = vi.hoisted(() => ({ superAgent: false }));
vi.mock("@/hooks/useLiveEvents", () => ({ useSuperAgent: () => live.superAgent }));

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

  it("leads with Threads only while the super-agent switch is on", () => {
    live.superAgent = true;
    try {
      render(<WorkTabs teamsKey="threads" />);
      expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual(["Threads", "Projects", "Goals", "Automations", "Cases", "Agents", "Money", "Older tasks"]);
      expect(screen.getByRole("tab", { name: "Threads" })).toHaveAttribute("aria-selected", "true");
    } finally { live.superAgent = false; }
  });

  it("covers exactly the pages the Work tab hosts", () => {
    expect([...WORK_TEAMS_KEYS]).toEqual(["issues", "projects", "goals", "routines", "cases", "agents", "money"]);
  });
});
