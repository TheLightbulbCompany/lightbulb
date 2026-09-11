// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, within } from "@testing-library/react";

const setPaletteOpen = vi.fn();
const openPanel = vi.fn();
const activateMessagesPanel = vi.fn();
let activePanelId = "browser";

vi.mock("@/components/teams/command-palette/CommandPaletteProvider", () => ({
  useCommandPalette: () => ({ open: false, setOpen: setPaletteOpen, toggle: vi.fn() }),
}));

vi.mock("@/components/support/SupportProvider", () => ({
  useSupport: () => ({ openSupport: vi.fn() }),
}));

vi.mock("@/components/ui/tooltip", () => ({
  Tooltip: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  TooltipTrigger: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  TooltipContent: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

vi.mock("@clerk/nextjs", () => {
  const UserButton = Object.assign(
    ({ children }: { children?: React.ReactNode }) => <div aria-label="Profile">{children}</div>,
    {
      MenuItems: ({ children }: { children: React.ReactNode }) => <>{children}</>,
      Link: () => null,
    },
  );
  return { UserButton };
});

vi.mock("../workspaceController", () => ({ openPanel: (...args: unknown[]) => openPanel(...args), activateMessagesPanel: () => activateMessagesPanel(), useWorkspaceState: () => ({ activePanelId }) }));

import { clearTeamRunActivity, trackTeamRunActivity } from "@/components/notifications/teamRunActivity";
import { WorkspaceHeader } from "../WorkspaceHeader";

afterEach(() => act(clearTeamRunActivity));

describe("WorkspaceHeader", () => {
  it.each([["Home", "teams-dashboard"], ["Activity", "teams-inbox"], ["Tasks", "teams-issues"], ["Browser", "browser"]])("opens %s and marks the active destination", (label, panel) => {
    activePanelId = panel;
    render(<WorkspaceHeader />);
    const button = screen.getByRole("button", { name: label });
    expect(button).toHaveAttribute("aria-current", "page");
    fireEvent.click(button);
    expect(openPanel).toHaveBeenCalledWith(panel, {}, { pushUrl: true });
  });
  it("shows active team runs on Activity without covering workspace content", () => {
    act(() => trackTeamRunActivity("teams.heartbeat.run.queued", { runId: "run-1" }));
    render(<WorkspaceHeader />);
    const activity = screen.getByRole("button", { name: "Activity, 1 active team run" });
    expect(within(activity).getByText("1")).toBeInTheDocument();
    fireEvent.click(activity);
    expect(openPanel).toHaveBeenCalledWith("teams-inbox", {}, { pushUrl: true });
  });
  it("returns to Messages and keeps settings accessible", () => {
    render(<WorkspaceHeader />);
    fireEvent.click(screen.getByRole("button", { name: "Messages" }));
    expect(activateMessagesPanel).toHaveBeenCalled();
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/settings");
  });
  it("disables panel navigation until provisioning completes", () => {
    render(<WorkspaceHeader chatReady={false} />);
    expect(screen.getByRole("button", { name: "Messages" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Browser" })).toBeDisabled();
  });
  it("opens the existing command palette from a visible search control", () => {
    render(<WorkspaceHeader />);

    fireEvent.click(screen.getByRole("button", { name: /Search agents, issues, and projects/ }));

    expect(setPaletteOpen).toHaveBeenCalledWith(true);
    expect(screen.getByRole("button", { name: /Search agents/ })).toHaveAttribute("title", "Search (⌘/Ctrl K)");
    expect(screen.getByRole("button", { name: "Browse workspace files" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Browser" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Get help" })).toBeInTheDocument();
    expect(screen.getByLabelText("Profile")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reset workspace layout" })).not.toBeInTheDocument();
  });
});
