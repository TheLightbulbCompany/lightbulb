// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { SWRConfig } from "swr";
import { fireEvent, render, screen } from "@testing-library/react";

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
const live = vi.hoisted(() => ({ tasks: null as unknown }));
vi.mock("@/hooks/useLiveEvents", () => ({ useOptionalLiveEvents: () => (live.tasks ? live : null) }));

import { WorkspaceHeader } from "../WorkspaceHeader";
import { loginAsk } from "@/components/activity/__tests__/fixtures";

describe("WorkspaceHeader", () => {

  it("records the Chat button as a Messages navigation", () => {
    render(<WorkspaceHeader chatReady={false} />);
    fireEvent.click(screen.getByRole("button", { name: "Chat" }));
    expect(activateMessagesPanel).toHaveBeenCalled();
    expect(recordMessagesNavigation).toHaveBeenCalled();
  });

  it.each([["Team", "team", "teams-agents"]])("opens %s and marks the current place", (label, current, panel) => {
    place = current;
    render(<WorkspaceHeader />);
    const button = screen.getByRole("button", { name: label });
    expect(button).toHaveAttribute("aria-current", "page");
    fireEvent.click(button);
    expect(openPanel).toHaveBeenCalledWith(panel, {}, { pushUrl: true });
  });
  it("Settings is a place in the shell (aria-current), not a link out of it", () => {
    place = "settings";
    try {
      render(<WorkspaceHeader />);
      const settings = screen.getByRole("button", { name: "Settings" });
      expect(settings).toHaveAttribute("aria-current", "page");
      expect(screen.queryByRole("link", { name: "Settings" })).toBeNull();
      fireEvent.click(settings);
      expect(openPanel).toHaveBeenCalledWith("settings", {}, { pushUrl: true });
    } finally { place = "activity"; }
  });
  it("reads Home, Activity, Chat, Team, Settings: no Work, no Creations, Library or Canvas", () => {
    place = "home";
    try {
      render(<WorkspaceHeader />);
      const rail = screen.getByRole("navigation", { name: "Workspace" });
      const labels = [...rail.querySelectorAll("button")].map((b) => b.getAttribute("aria-label"));
      expect(labels.slice(labels.indexOf("Home"), labels.indexOf("Team") + 1)).toEqual(["Home", "Activity", "Chat", "Team"]);
      expect(labels).toContain("Settings");
      expect(labels).not.toContain("Work");
      expect(screen.getByRole("button", { name: "Home" })).toHaveAttribute("aria-current", "page");
      fireEvent.click(screen.getByRole("button", { name: "Activity" }));
      expect(openPanel).toHaveBeenCalledWith("activity", {}, { pushUrl: true });
      fireEvent.click(screen.getByRole("button", { name: "Home" }));
      expect(openPanel).toHaveBeenCalledWith("home", {}, { pushUrl: true });
      fireEvent.click(screen.getByRole("button", { name: "Chat" }));
      expect(activateMessagesPanel).toHaveBeenCalled();
    } finally { place = "activity"; }
  });
  it("a channel or DM (the Chat place) lights Chat on the rail (founder, 6 Nov 2026)", () => {
    place = "chat";
    try {
      render(<WorkspaceHeader />);
      const rail = screen.getByRole("navigation", { name: "Workspace" });
      expect(rail.querySelector('[aria-current="page"]')).toHaveAttribute("aria-label", "Chat");
    } finally { place = "activity"; }
  });
  it("Activity badges exactly its Needs you count: not the legacy badge, not team runs", async () => {
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
    } finally { live.tasks = null; activityState.attentionCount = 0; activityItems.items = []; }
  });
});
