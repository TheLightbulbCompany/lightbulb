// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { SWRConfig } from "swr";
import { act, fireEvent, render, screen, within } from "@testing-library/react";

const setPaletteOpen = vi.fn();
const openPanel = vi.fn();
const activateMessagesPanel = vi.fn();
let place = "activity";

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
  // useNeedsYouCount's first-move read waits for auth: none here.
  return { UserButton, useAuth: () => ({ isLoaded: false, userId: null, orgId: null }) };
});

const activityState = { attentionCount: 0 };
const activityItems = vi.hoisted(() => ({ items: [] as unknown[] }));
vi.mock("@/hooks/useActivity", () => ({ useActivity: () => activityItems }));
vi.mock("@/components/activity/useNeedsYouBadge", () => ({ useNeedsYouBadge: () => activityState.attentionCount }));
const closePane = vi.fn();
vi.mock("../workspaceController", () => ({ openPanel: (...args: unknown[]) => openPanel(...args), activateMessagesPanel: () => activateMessagesPanel(), closePane: () => closePane(), useWorkspaceState: () => ({ place }) }));

const apiGet = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: apiGet }) }));

const recordMessagesNavigation = vi.fn();
vi.mock("../chatHistory", () => ({ recordMessagesNavigation: () => recordMessagesNavigation() }));
const live = vi.hoisted(() => ({ superAgent: false, tasks: null as unknown }));
vi.mock("@/hooks/useLiveEvents", () => ({ useSuperAgent: () => live.superAgent, useOptionalLiveEvents: () => (live.tasks ? live : null) }));

import { trackTeamRunActivity } from "@/components/notifications/teamRunActivity";
import { WorkspaceHeader } from "../WorkspaceHeader";
import { loginAsk } from "@/components/activity/__tests__/fixtures";

describe("WorkspaceHeader", () => {
  // The six places and nothing else (spec 3.1): Browser, Files and Mail open in the right
  // pane; Hire, Invite and Connections each have a home elsewhere. Home took Activity's place, first.
  it("reads Home, Chat, Work, Team, Library, Canvas, in that order, and nothing else", () => {
    render(<WorkspaceHeader />);
    const rail = screen.getByRole("navigation", { name: "Workspace" });
    const labels = [...rail.querySelectorAll("button")].map((b) => b.getAttribute("aria-label"));
    expect(labels.slice(labels.indexOf("Home"), labels.indexOf("Canvas") + 1)).toEqual(["Home", "Chat", "Work", "Team", "Library", "Canvas"]);
    for (const gone of ["Mail", "Browser", "Files", "Activity", "Messages", "Hire an agent", "Invite people", "Connections"]) {
      expect(screen.queryByRole("button", { name: gone })).toBeNull();
    }
  });

  it("records the Chat button as a Messages navigation", () => {
    render(<WorkspaceHeader chatReady={false} />);
    fireEvent.click(screen.getByRole("button", { name: "Chat" }));
    expect(activateMessagesPanel).toHaveBeenCalled();
    expect(recordMessagesNavigation).toHaveBeenCalled();
  });

  it.each([["Home", "activity", "activity"], ["Team", "team", "teams-agents"], ["Library", "library", "teams-artifacts"]])("opens %s and marks the current place", (label, current, panel) => {
    place = current;
    render(<WorkspaceHeader />);
    const button = screen.getByRole("button", { name: label });
    expect(button).toHaveAttribute("aria-current", "page");
    fireEvent.click(button);
    expect(openPanel).toHaveBeenCalledWith(panel, {}, { pushUrl: true });
  });
  it("opens Work on Tasks and marks it active while any Work page is open", () => {
    place = "work";
    render(<WorkspaceHeader />);
    const work = screen.getByRole("button", { name: "Work" });
    expect(work).toHaveAttribute("aria-current", "page");
    fireEvent.click(work);
    expect(openPanel).toHaveBeenCalledWith("teams-issues", {}, { pushUrl: true });
  });
  it("with the switch on, Settings is a place in the shell (aria-current), not a link out of it", () => {
    live.superAgent = true;
    place = "settings";
    try {
      render(<WorkspaceHeader />);
      const settings = screen.getByRole("button", { name: "Settings" });
      expect(settings).toHaveAttribute("aria-current", "page");
      expect(screen.queryByRole("link", { name: "Settings" })).toBeNull();
      fireEvent.click(settings);
      expect(openPanel).toHaveBeenCalledWith("settings", {}, { pushUrl: true });
    } finally { live.superAgent = false; place = "activity"; }
  });
  it("closes the right pane when it moves to another place, not when the place is the current one", () => {
    place = "activity";
    closePane.mockClear();
    render(<WorkspaceHeader />);
    fireEvent.click(screen.getByRole("button", { name: "Home" }));
    expect(closePane).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Work" }));
    expect(closePane).toHaveBeenCalledOnce();
  });
  it("with the switch on, reads Home, Activity, Creations, Team, Settings: no Chat, no Work, no Library or Canvas; a project lights nothing", () => {
    live.superAgent = true;
    place = "home";
    try {
      render(<WorkspaceHeader />);
      const rail = screen.getByRole("navigation", { name: "Workspace" });
      const labels = [...rail.querySelectorAll("button")].map((b) => b.getAttribute("aria-label"));
      expect(labels.slice(labels.indexOf("Home"), labels.indexOf("Team") + 1)).toEqual(["Home", "Activity", "Creations", "Team"]);
      expect(labels).toContain("Settings");
      expect(labels).not.toContain("Chat");
      expect(labels).not.toContain("Work");
      expect(screen.getByRole("button", { name: "Home" })).toHaveAttribute("aria-current", "page");
      fireEvent.click(screen.getByRole("button", { name: "Activity" }));
      expect(openPanel).toHaveBeenCalledWith("activity", {}, { pushUrl: true });
      fireEvent.click(screen.getByRole("button", { name: "Home" }));
      expect(openPanel).toHaveBeenCalledWith("home", {}, { pushUrl: true });
    } finally { live.superAgent = false; place = "activity"; }
  });
  it("with the switch on, a project (the Chat place) lights no rail place", () => {
    live.superAgent = true;
    place = "chat";
    try {
      render(<WorkspaceHeader />);
      const rail = screen.getByRole("navigation", { name: "Workspace" });
      expect(rail.querySelector('[aria-current="page"]')).toBeNull();
    } finally { live.superAgent = false; place = "activity"; }
  });
  it("with the switch on, Activity badges exactly its Needs you count: not the legacy badge, not team runs", async () => {
    live.superAgent = true;
    // A website sign-in ask is not a task, but it needs you all the same: one count (UX audit slice 2).
    activityItems.items = [loginAsk];
    activityState.attentionCount = 1;
    const t = (taskId: string, shownGroup: string) => ({ taskId, shownGroup, status: "waiting", watching: false, updatedAt: "2026-11-05T00:00:00Z" });
    live.tasks = { cacheScope: "org_h" };
    // The complete Needs you read (server-filtered; a stray working row is cut client-side by the same rule).
    apiGet.mockResolvedValue({ next: null, tasks: [t("a", "waiting_on_you"), t("b", "ready_for_review"), t("c", "working")] });
    try {
      render(<SWRConfig value={{ provider: () => new Map() }}><WorkspaceHeader /></SWRConfig>);
      expect(await screen.findByRole("button", { name: "Activity, 3 need you" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Home" })).toBeInTheDocument();
    } finally { live.superAgent = false; live.tasks = null; activityState.attentionCount = 0; activityItems.items = []; }
  });
  it("shows active team runs on Work without covering workspace content", () => {
    act(() => trackTeamRunActivity("teams.heartbeat.run.queued", { runId: "run-1" }));
    render(<WorkspaceHeader />);
    const tasks = screen.getByRole("button", { name: "Work, 1 active team run" });
    expect(within(tasks).getByText("1")).toBeInTheDocument();
    fireEvent.click(tasks);
    expect(openPanel).toHaveBeenCalledWith("teams-issues", {}, { pushUrl: true });

    // Resolve the run so it doesn't leak into the next test's activity count.
    act(() => trackTeamRunActivity("teams.heartbeat.run.status", { runId: "run-1", status: "succeeded" }));
  });
  it("badges Home with what needs you (approvals, failed runs, unseen)", () => {
    activityState.attentionCount = 3;
    render(<WorkspaceHeader />);
    const activity = screen.getByRole("button", { name: "Home, 3 need you" });
    expect(within(activity).getByText("3")).toBeInTheDocument();
    fireEvent.click(activity);
    expect(openPanel).toHaveBeenCalledWith("activity", {}, { pushUrl: true });
    activityState.attentionCount = 0;
  });
  it("returns to Chat and keeps settings accessible", () => {
    render(<WorkspaceHeader />);
    fireEvent.click(screen.getByRole("button", { name: "Chat" }));
    expect(activateMessagesPanel).toHaveBeenCalled();
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/settings");
  });
  it("keeps Chat available but disables runtime-backed places until the agents are up", () => {
    render(<WorkspaceHeader chatReady={false} />);
    expect(screen.getByRole("button", { name: "Chat" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Work" })).toBeDisabled();
  });
  it("opens the existing command palette from a visible search control", () => {
    render(<WorkspaceHeader />);

    fireEvent.click(screen.getByRole("button", { name: /Search messages, agents, issues, and projects/ }));

    expect(setPaletteOpen).toHaveBeenCalledWith(true);
    expect(screen.getByRole("button", { name: /Search messages/ })).toHaveAttribute("title", "Search (⌘/Ctrl K)");
    expect(screen.getByRole("button", { name: "Get help" })).toBeInTheDocument();
    expect(screen.getByLabelText("Profile")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reset workspace layout" })).not.toBeInTheDocument();
  });
});
