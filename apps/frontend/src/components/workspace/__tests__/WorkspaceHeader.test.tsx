// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, within } from "@testing-library/react";

const setPaletteOpen = vi.fn();
const openPanel = vi.fn();
const activateMessagesPanel = vi.fn();
let activePanelId = "teams-dashboard";
const shared = vi.hoisted(() => ({ statusLoaded: false }));

// Partial mock: WorkspaceHeader reads only `useStatusLoaded` from the barrel, but
// the rest of this module graph pulls plenty else out of it.
vi.mock("@lightbulb/shared", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useStatusLoaded: () => shared.statusLoaded,
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

const openMarketplace = vi.fn();
vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({
  useMarketplaceDrawer: () => ({ open: openMarketplace }),
}));

const activityState = { attentionCount: 0 };
vi.mock("@/hooks/useActivity", () => ({ useActivity: () => activityState }));
const mailState = { newCount: 0 };
vi.mock("@/components/mail/useTeamMail", () => ({ useNewMailCount: () => mailState.newCount }));
const openInviteDialog = vi.fn();
vi.mock("../workspaceController", () => ({ openPanel: (...args: unknown[]) => openPanel(...args), activateMessagesPanel: () => activateMessagesPanel(), openInviteDialog: () => openInviteDialog(), useWorkspaceState: () => ({ activePanelId }) }));

const recordMessagesNavigation = vi.fn();
vi.mock("../chatHistory", () => ({ recordMessagesNavigation: () => recordMessagesNavigation() }));

import { trackTeamRunActivity } from "@/components/notifications/teamRunActivity";
import { WorkspaceHeader } from "../WorkspaceHeader";

describe("WorkspaceHeader", () => {
  it("records the Messages button as a navigation", () => {
    render(<WorkspaceHeader chatReady={false} />);
    fireEvent.click(screen.getByRole("button", { name: "Messages" }));
    expect(activateMessagesPanel).toHaveBeenCalled();
    expect(recordMessagesNavigation).toHaveBeenCalled();
  });

  it("offers Hire right after Tasks and opens the marketplace, not a panel", () => {
    render(<WorkspaceHeader chatReady={false} />);
    const labels = within(screen.getByRole("navigation", { name: "Workspace" }))
      .getAllByRole("button")
      .map((b) => b.getAttribute("aria-label"));
    expect(labels.indexOf("Hire an agent")).toBe(labels.indexOf("Tasks") + 1);
    const hire = screen.getByRole("button", { name: "Hire an agent" });
    expect(hire).not.toBeDisabled();
    expect(screen.getByRole("button", { name: "Mail" })).not.toBeDisabled(); // the mailbox needs no runtime
    openPanel.mockClear();
    fireEvent.click(hire);
    expect(openMarketplace).toHaveBeenCalledWith({ view: "browse" });
    expect(openPanel).not.toHaveBeenCalled();
  });

  it("puts Mail right after Activity", () => {
    render(<WorkspaceHeader />);
    const labels = within(screen.getByRole("navigation", { name: "Workspace" }))
      .getAllByRole("button")
      .map((b) => b.getAttribute("aria-label"));
    expect(labels.indexOf("Mail")).toBe(labels.indexOf("Activity") + 1);
  });

  it("badges Mail with new received mail, capped at 9+", () => {
    mailState.newCount = 12;
    render(<WorkspaceHeader />);
    const mail = screen.getByRole("button", { name: "Mail, 12 new" });
    expect(within(mail).getByText("9+")).toBeInTheDocument();
    fireEvent.click(mail);
    expect(openPanel).toHaveBeenCalledWith("mail", {}, { pushUrl: true });
    mailState.newCount = 0;
  });

  it.each([["Home", "teams-dashboard"], ["Activity", "activity"], ["Mail", "mail"], ["Tasks", "teams-issues"], ["Library", "teams-artifacts"]])("opens %s and marks the active destination", (label, panel) => {
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
  // status body has landed, so an unresolved status body hides it rather than
  // offering a panel that can only render its empty state.
  it("offers Browser only on an AgentCore workspace", () => {
    shared.statusLoaded = false;
    const bare = render(<WorkspaceHeader />);
    expect(screen.queryByRole("button", { name: "Browser" })).toBeNull();
    bare.unmount();

    shared.statusLoaded = true;
    render(<WorkspaceHeader />);
    fireEvent.click(screen.getByRole("button", { name: "Browser" }));
    expect(openPanel).toHaveBeenCalledWith("browser", {}, { pushUrl: true });
    shared.statusLoaded = false;
  });

  it("shows active team runs on Tasks without covering workspace content", () => {
    act(() => trackTeamRunActivity("teams.heartbeat.run.queued", { runId: "run-1" }));
    render(<WorkspaceHeader />);
    const tasks = screen.getByRole("button", { name: "Tasks, 1 active team run" });
    expect(within(tasks).getByText("1")).toBeInTheDocument();
    fireEvent.click(tasks);
    expect(openPanel).toHaveBeenCalledWith("teams-issues", {}, { pushUrl: true });

    // Resolve the run so it doesn't leak into the next test's activity count.
    act(() => trackTeamRunActivity("teams.heartbeat.run.status", { runId: "run-1", status: "succeeded" }));
  });
  it("badges Activity with what needs you (approvals, failed runs, unseen)", () => {
    activityState.attentionCount = 3;
    render(<WorkspaceHeader />);
    const activity = screen.getByRole("button", { name: "Activity, 3 need you" });
    expect(within(activity).getByText("3")).toBeInTheDocument();
    fireEvent.click(activity);
    expect(openPanel).toHaveBeenCalledWith("activity", {}, { pushUrl: true });
    activityState.attentionCount = 0;
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

    fireEvent.click(screen.getByRole("button", { name: /Search messages, agents, issues, and projects/ }));

    expect(setPaletteOpen).toHaveBeenCalledWith(true);
    expect(screen.getByRole("button", { name: /Search messages/ })).toHaveAttribute("title", "Search (⌘/Ctrl K)");
    expect(screen.getByRole("button", { name: "Browse workspace files" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Get help" })).toBeInTheDocument();
    expect(screen.getByLabelText("Profile")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reset workspace layout" })).not.toBeInTheDocument();
  });

  it("offers Invite people to everyone, even before chat is ready, and opens the invite dialog", () => {
    const onNavigate = vi.fn();
    render(<WorkspaceHeader chatReady={false} onNavigate={onNavigate} />);
    const invite = screen.getByRole("button", { name: "Invite people" });
    expect(invite).not.toBeDisabled();
    fireEvent.click(invite);
    expect(openInviteDialog).toHaveBeenCalledTimes(1);
    expect(onNavigate).toHaveBeenCalled();
  });
});
