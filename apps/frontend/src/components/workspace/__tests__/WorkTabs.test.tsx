// @vitest-environment jsdom
// The Work page: every task-board page is a tab of one dock tab, "Work".
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

const openPanel = vi.fn();
vi.mock("../workspaceController", () => ({ openPanel: (...a: unknown[]) => openPanel(...a) }));
const live = vi.hoisted(() => ({ superAgent: false }));
vi.mock("@/hooks/useLiveEvents", () => ({ useSuperAgent: () => live.superAgent }));
const issues = vi.hoisted(() => ({ list: [{ id: "i1" }] as unknown[] }));
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read: (path: string | null) => ({ data: path ? { issues: issues.list } : undefined }) }) }));

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
      // Claude-Projects simple: no Projects / Goals / Cases; the old task board last, while it holds any.
      expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual(["Threads", "Automations", "Agents", "Money", "Older tasks"]);
      expect(screen.getByRole("tab", { name: "Threads" })).toHaveAttribute("aria-selected", "true");
    } finally { live.superAgent = false; }
  });

  it("switch on: no Older tasks tab without old tasks; a hidden page opened by link keeps its own tab", () => {
    live.superAgent = true;
    issues.list = [];
    try {
      const { unmount } = render(<WorkTabs teamsKey="threads" />);
      expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual(["Threads", "Automations", "Agents", "Money"]);
      unmount();
      render(<WorkTabs teamsKey="goals" />);
      expect(screen.getByRole("tab", { name: "Goals" })).toHaveAttribute("aria-selected", "true");
    } finally { live.superAgent = false; issues.list = [{ id: "i1" }]; }
  });

  it("covers exactly the pages the Work tab hosts", () => {
    expect([...WORK_TEAMS_KEYS]).toEqual(["issues", "projects", "goals", "routines", "cases", "agents", "money"]);
  });
});
