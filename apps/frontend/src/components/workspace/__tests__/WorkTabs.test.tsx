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
    expect(tabs.map((t) => t.textContent)).toEqual(["Tasks", "Projects", "Goals", "Automations", "Cases", "Money"]);
    expect(screen.getByRole("tab", { name: "Projects" })).toHaveAttribute("aria-selected", "true");
  });

  it("switches page inside the same Work tab", () => {
    render(<WorkTabs teamsKey="issues" />);
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Goals" }), { button: 0 });
    expect(openPanel).toHaveBeenCalledWith("teams-goals", {}, { pushUrl: true });
  });

  it("switch on: requests live on Home, Agents on Team; Work keeps Automations and Money", () => {
    live.superAgent = true;
    try {
      render(<WorkTabs teamsKey="routines" />);
      expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual(["Automations", "Money"]);
    } finally { live.superAgent = false; }
  });

  it("switch on: a hidden page opened by link keeps its own tab (the old board as Older tasks)", () => {
    live.superAgent = true;
    try {
      const { unmount } = render(<WorkTabs teamsKey="goals" />);
      expect(screen.getByRole("tab", { name: "Goals" })).toHaveAttribute("aria-selected", "true");
      unmount();
      render(<WorkTabs teamsKey="issues" />);
      expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual(["Older tasks", "Automations", "Money"]);
    } finally { live.superAgent = false; }
  });

  it("covers exactly the pages the Work tab hosts", () => {
    expect([...WORK_TEAMS_KEYS]).toEqual(["issues", "projects", "goals", "routines", "cases", "money"]);
  });
});
