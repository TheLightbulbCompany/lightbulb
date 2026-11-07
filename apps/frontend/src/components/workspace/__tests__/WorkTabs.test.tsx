// @vitest-environment jsdom
// The Work page: every task-board page is a tab of one dock tab, "Work".
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

const openPanel = vi.fn();
vi.mock("../workspaceController", () => ({ openPanel: (...a: unknown[]) => openPanel(...a) }));

import { WorkTabs, WORK_TEAMS_KEYS } from "../panels/WorkTabs";

describe("WorkTabs", () => {
  beforeEach(() => openPanel.mockClear());

  it("switches page inside the same Work tab", () => {
    render(<WorkTabs teamsKey="issues" />);
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Automations" }), { button: 0 });
    expect(openPanel).toHaveBeenCalledWith("teams-routines", {}, { pushUrl: true });
  });

  it("requests live on Home, Agents on Team; Work keeps Automations and Money", () => {
    render(<WorkTabs teamsKey="routines" />);
    expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual(["Automations", "Money"]);
  });

  it("a hidden page opened by link keeps its own tab (the old board as Older tasks)", () => {
    const { unmount } = render(<WorkTabs teamsKey="goals" />);
    expect(screen.getByRole("tab", { name: "Goals" })).toHaveAttribute("aria-selected", "true");
    unmount();
    render(<WorkTabs teamsKey="issues" />);
    expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual(["Older tasks", "Automations", "Money"]);
  });

  it("covers exactly the pages the Work tab hosts", () => {
    expect([...WORK_TEAMS_KEYS]).toEqual(["issues", "projects", "goals", "routines", "cases", "money"]);
  });
});
