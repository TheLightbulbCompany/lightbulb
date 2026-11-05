// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
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
  return { UserButton };
});

const activityState = { attentionCount: 0 };
vi.mock("@/components/activity/useNeedsYouBadge", () => ({ useNeedsYouBadge: () => activityState.attentionCount }));
vi.mock("../workspaceController", () => ({ openPanel: (...args: unknown[]) => openPanel(...args), activateMessagesPanel: () => activateMessagesPanel(), useWorkspaceState: () => ({ place }) }));

const recordMessagesNavigation = vi.fn();
vi.mock("../chatHistory", () => ({ recordMessagesNavigation: () => recordMessagesNavigation() }));
const live = vi.hoisted(() => ({ superAgent: false, tasks: null as unknown }));
vi.mock("@/hooks/useLiveEvents", () => ({ useSuperAgent: () => live.superAgent, useOptionalLiveEvents: () => (live.tasks ? live : null) }));

import { trackTeamRunActivity } from "@/components/notifications/teamRunActivity";
import { WorkspaceHeader } from "../WorkspaceHeader";

describe("WorkspaceHeader", () => {
  // The five places and nothing else (spec 3.1): Browser, Files and Mail open in the right
  // pane; Home, Hire, Invite and Connections each have a home elsewhere.
  it("reads Chat, Activity, Work, Library, Canvas, in that order, and nothing else", () => {
    render(<WorkspaceHeader />);
    const rail = screen.getByRole("navigation", { name: "Workspace" });
    const labels = [...rail.querySelectorAll("button")].map((b) => b.getAttribute("aria-label"));
    expect(labels.slice(labels.indexOf("Chat"), labels.indexOf("Canvas") + 1)).toEqual(["Chat", "Activity", "Work", "Library", "Canvas"]);
    for (const gone of ["Mail", "Browser", "Files", "Home", "Messages", "Hire an agent", "Invite people", "Connections"]) {
      expect(screen.queryByRole("button", { name: gone })).toBeNull();
    }
  });

  it("records the Chat button as a Messages navigation", () => {
    render(<WorkspaceHeader chatReady={false} />);
    fireEvent.click(screen.getByRole("button", { name: "Chat" }));
    expect(activateMessagesPanel).toHaveBeenCalled();
    expect(recordMessagesNavigation).toHaveBeenCalled();
  });

  it.each([["Activity", "activity", "activity"], ["Library", "library", "teams-artifacts"]])("opens %s and marks the current place", (label, current, panel) => {
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
  it("opens Work on Threads while the super-agent switch is on", () => {
    live.superAgent = true;
    try {
      render(<WorkspaceHeader />);
      fireEvent.click(screen.getByRole("button", { name: "Work" }));
      expect(openPanel).toHaveBeenCalledWith("teams-threads", {}, { pushUrl: true });
    } finally { live.superAgent = false; }
  });
  it("with the switch on, Work badges the threads waiting on you, not team runs", async () => {
    live.superAgent = true;
    const { fixtureTask } = await import("@lightbulb/shared/tasks/fixtures");
    live.tasks = { cacheScope: "org_h", list: vi.fn(async () => ({ next: null, tasks: [
      fixtureTask({ taskId: "a", title: "a", shownGroup: "waiting_on_you" }), fixtureTask({ taskId: "b", title: "b", shownGroup: "working" }),
    ] })) };
    try {
      render(<WorkspaceHeader />);
      expect(await screen.findByRole("button", { name: "Work, 1 thread waiting on you" })).toBeInTheDocument();
    } finally { live.superAgent = false; live.tasks = null; }
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
  it("badges Activity with what needs you (approvals, failed runs, unseen)", () => {
    activityState.attentionCount = 3;
    render(<WorkspaceHeader />);
    const activity = screen.getByRole("button", { name: "Activity, 3 need you" });
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
