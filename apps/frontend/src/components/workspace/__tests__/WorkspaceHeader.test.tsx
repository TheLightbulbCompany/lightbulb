// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, within } from "@testing-library/react";

const setPaletteOpen = vi.fn();
const openPanel = vi.fn();
const activateMessagesPanel = vi.fn();
let activePanelId = "teams-dashboard";
const shared = vi.hoisted(() => ({ runtimeHost: undefined as "agentcore" | undefined }));

// Partial mock: WorkspaceHeader reads only `useRuntimeHost` from the barrel, but
// the rest of this module graph pulls plenty else out of it.
vi.mock("@lightbulb/shared", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useRuntimeHost: () => shared.runtimeHost,
}));

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

import { trackTeamRunActivity } from "@/components/notifications/teamRunActivity";
import { WorkspaceHeader } from "../WorkspaceHeader";

describe("WorkspaceHeader", () => {
  it.each([["Home", "teams-dashboard"], ["Activity", "teams-inbox"], ["Tasks", "teams-issues"]])("opens %s and marks the active destination", (label, panel) => {
    activePanelId = panel;
    render(<WorkspaceHeader />);
    const button = screen.getByRole("button", { name: label });
    expect(button).toHaveAttribute("aria-current", "page");
    fireEvent.click(button);
    expect(openPanel).toHaveBeenCalledWith(panel, {}, { pushUrl: true });
  });
  // Connections opens as a CONTROL-key panel, so its dock id ("control:connections")
  // is not its rail key — the active marker has to resolve through the registry
  // or it silently never lights up.
  it("opens Connections under Files and marks it active by its dock id", () => {
    activePanelId = "control:connections";
    render(<WorkspaceHeader />);
    const button = screen.getByRole("button", { name: "Connections" });
    expect(button).toHaveAttribute("aria-current", "page");
    fireEvent.click(button);
    expect(openPanel).toHaveBeenCalledWith("connections", {}, { pushUrl: true });

    // Order matters: the ask was "under workspace files".
    const rail = screen.getByRole("navigation", { name: "Workspace" });
    const labels = [...rail.querySelectorAll("button")].map((b) => b.getAttribute("aria-label"));
    const filesAt = labels.indexOf("Browse workspace files");
    expect(labels.slice(filesAt, filesAt + 2)).toEqual([
      "Browse workspace files",
      "Connections",
    ]);
  });
  // Inverted gate: the AgentCore Browser live view is offered only once the
  // runtime host is KNOWN, so an unresolved status body hides it rather than
  // offering a panel that can only render its empty state.
  it("offers Browser only on an AgentCore workspace", () => {
    shared.runtimeHost = undefined;
    const bare = render(<WorkspaceHeader />);
    expect(screen.queryByRole("button", { name: "Browser" })).toBeNull();
    bare.unmount();

    shared.runtimeHost = "agentcore";
    render(<WorkspaceHeader />);
    fireEvent.click(screen.getByRole("button", { name: "Browser" }));
    expect(openPanel).toHaveBeenCalledWith("browser", {}, { pushUrl: true });
    shared.runtimeHost = undefined;
  });

  it("shows active team runs on Activity without covering workspace content", () => {
    act(() => trackTeamRunActivity("teams.heartbeat.run.queued", { runId: "run-1" }));
    render(<WorkspaceHeader />);
    const activity = screen.getByRole("button", { name: "Activity, 1 active team run" });
    expect(within(activity).getByText("1")).toBeInTheDocument();
    fireEvent.click(activity);
    expect(openPanel).toHaveBeenCalledWith("teams-inbox", {}, { pushUrl: true });

    // Resolve the run so it doesn't leak into the next test's activity count.
    act(() => trackTeamRunActivity("teams.heartbeat.run.status", { runId: "run-1", status: "succeeded" }));
  });
  it("returns to Messages and keeps settings accessible", () => {
    render(<WorkspaceHeader />);
    fireEvent.click(screen.getByRole("button", { name: "Messages" }));
    expect(activateMessagesPanel).toHaveBeenCalled();
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/settings");
  });
  it("keeps Messages available but disables runtime-backed panels until the agents are up", () => {
    render(<WorkspaceHeader chatReady={false} />);
    expect(screen.getByRole("button", { name: "Messages" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Browse workspace files" })).toBeDisabled();
  });
  it("opens the existing command palette from a visible search control", () => {
    render(<WorkspaceHeader />);

    fireEvent.click(screen.getByRole("button", { name: /Search agents, issues, and projects/ }));

    expect(setPaletteOpen).toHaveBeenCalledWith(true);
    expect(screen.getByRole("button", { name: /Search agents/ })).toHaveAttribute("title", "Search (⌘/Ctrl K)");
    expect(screen.getByRole("button", { name: "Browse workspace files" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Get help" })).toBeInTheDocument();
    expect(screen.getByLabelText("Profile")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reset workspace layout" })).not.toBeInTheDocument();
  });
});
