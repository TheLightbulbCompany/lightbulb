// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import type { IDockviewHeaderActionsProps } from "dockview-react";

vi.mock("../workspaceController", () => ({
  TASKS_PANEL_ID: "teams-issues",
  canSplitBesideMessages: vi.fn(() => true),
  setTasksPlacement: vi.fn(),
}));
import { canSplitBesideMessages, setTasksPlacement } from "../workspaceController";
import { TasksHeaderActions } from "../TasksHeaderActions";
import { MESSAGES_PANEL_ID } from "../panelRegistry";

function props(activeId: string, groupId: string, messagesGroupId: string | null) {
  return {
    activePanel: { id: activeId },
    group: { id: groupId },
    containerApi: {
      getPanel: (id: string) => (id === MESSAGES_PANEL_ID && messagesGroupId ? { group: { id: messagesGroupId } } : undefined),
    },
  } as unknown as IDockviewHeaderActionsProps;
}

beforeEach(() => vi.clearAllMocks());

describe("TasksHeaderActions", () => {
  it("offers Expand when Tasks sits beside the conversation", () => {
    render(<TasksHeaderActions {...props("teams-issues", "right", "main")} />);
    fireEvent.click(screen.getByRole("button", { name: "Expand to a full tab" }));
    expect(setTasksPlacement).toHaveBeenCalledWith("tab");
  });

  it("offers Show beside chat when Tasks is a tab in the Messages group", () => {
    render(<TasksHeaderActions {...props("teams-issues", "main", "main")} />);
    fireEvent.click(screen.getByRole("button", { name: "Show beside chat" }));
    expect(setTasksPlacement).toHaveBeenCalledWith("split");
  });

  it("hides Show beside chat when the dock cannot split", () => {
    vi.mocked(canSplitBesideMessages).mockReturnValueOnce(false);
    const { container } = render(<TasksHeaderActions {...props("teams-issues", "main", "main")} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing for other panels or without Messages", () => {
    const a = render(<TasksHeaderActions {...props("teams-inbox", "main", "main")} />);
    expect(a.container).toBeEmptyDOMElement();
    const b = render(<TasksHeaderActions {...props("teams-issues", "main", null)} />);
    expect(b.container).toBeEmptyDOMElement();
  });
});
