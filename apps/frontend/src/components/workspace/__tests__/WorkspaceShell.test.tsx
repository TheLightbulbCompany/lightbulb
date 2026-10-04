// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { WorkspaceShell } from "../WorkspaceShell";

const state = vi.hoisted(() => ({
  phase: "selecting", selectProps: vi.fn(), organization: null as { id: string } | null,
  openPanelIds: [] as string[], activePanelId: "chat:__default__", chatReady: true,
  // null = single group: Messages is visible exactly when it is active.
  split: null as null | { messagesVisible: boolean; teamsPanelVisible: boolean },
}));
vi.mock("@/hooks/useOnboardingGate", () => ({ useOnboardingGate: () => state.phase }));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ isSignedIn: true, userId: "user_1" }),
  useOrganization: () => ({ organization: state.organization }),
  OrganizationList: (props: unknown) => { state.selectProps(props); return null; },
}));
// An owner-aware child must never mount without an active workspace.
vi.mock("@/hooks/useAgents", () => ({ useAgents: () => {
  if (!state.organization) throw new Error("Owner data requested without a workspace");
  return { agents: [], defaultId: undefined, isSettled: true };
} }));
vi.mock("next/navigation", () => ({ useSearchParams: () => null }));
vi.mock("../workspaceController", () => ({
  useWorkspaceState: () => ({
    activePanelId: state.activePanelId,
    openPanelIds: state.openPanelIds,
    messagesVisible: state.split?.messagesVisible ?? state.activePanelId === "chat:__default__",
    teamsPanelVisible: state.split?.teamsPanelVisible ?? state.activePanelId.startsWith("teams-"),
  }),
  refreshWorkspaceSnapshot: vi.fn(),
  refitSplits: vi.fn(),
  getWorkspaceSnapshot: () => ({ ready: false }),
}));
vi.mock("@/components/messaging/MessagingWorkspaceContext", () => ({
  MessagingWorkspaceProvider: ({ children, subscribeToRoom, observeRuns }: { children: ReactNode; subscribeToRoom: boolean; observeRuns?: boolean }) => (
    <section aria-label="Messaging owner" data-subscribed={subscribeToRoom} data-observe-runs={observeRuns}>{children}</section>
  ),
}));
vi.mock("../WorkspaceSidebar", () => ({
  WorkspaceSidebar: ({ onRecoveryReprovision, chatReady, onNavigate }: { onRecoveryReprovision: () => void; chatReady: boolean; onNavigate: () => void }) => (
    <nav aria-label="Workspace">
      <button onClick={onRecoveryReprovision} data-ready={chatReady}>Recover</button>
      <button onClick={onNavigate}>Open a room</button>
    </nav>
  ),
}));
vi.mock("../WorkspaceHeader", () => ({ WorkspaceHeader: () => <nav className="workspace-rail"><button>Search</button></nav> }));
vi.mock("dockview-react", () => ({
  themeLight: { name: "light", className: "dockview-theme-light" },
  DockviewReact: ({ disableDnd }: { disableDnd: boolean }) => <div aria-label="Dock" data-dnd-disabled={disableDnd} />,
}));
vi.mock("@/components/chat/ProvisioningStepper", () => ({
  ProvisioningStepper: ({ children, trigger }: { children: ReactNode; trigger?: string }) => <div data-testid="stepper" data-trigger={trigger}>{children}</div>,
}));
vi.mock("@/components/chat/InlineTeamsNav", () => ({ InlineTeamsNavProvider: ({ children }: { children: ReactNode }) => children }));
vi.mock("@/components/teams/TeamsEventsProvider", () => ({ TeamsEventsProvider: ({ children }: { children: ReactNode }) => children }));
vi.mock("@/components/teams/command-palette/CommandPaletteProvider", () => ({ CommandPaletteProvider: ({ children }: { children: ReactNode }) => children }));
vi.mock("@/components/teams/TeamsPanelRouter", () => ({ preloadTeamsPanels: vi.fn() }));
vi.mock("@/components/desktop/DesktopPermissionsGate", () => ({ DesktopPermissionsGate: () => null }));
vi.mock("@/components/chat/TrialBanner", () => ({ TrialBanner: () => null }));
vi.mock("@/components/chat/OutOfCreditsBanner", () => ({ OutOfCreditsBanner: () => null }));
vi.mock("@/components/chat/LaunchJoinBanner", () => ({ LaunchJoinBanner: () => null }));
vi.mock("@/components/chat/LaunchJoinModal", () => ({ LaunchJoinModal: () => null }));
vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({ useMarketplaceDrawer: () => ({ open: vi.fn() }) }));
vi.mock("@/components/chat/shell/useUserSyncOnce", () => ({ useUserSyncOnce: () => {} }));
vi.mock("@/components/chat/shell/useCheckoutReturn", () => ({ useCheckoutReturn: () => ({ showSubscriptionSuccess: true }) }));
vi.mock("@/components/chat/shell/useChatReady", () => ({ useChatReady: () => ({ chatReady: state.chatReady }) }));
beforeEach(() => {
  state.openPanelIds = [];
  state.phase = "selecting";
  state.organization = null;
  state.activePanelId = "chat:__default__";
  state.chatReady = true;
  state.split = null;
  vi.clearAllMocks();
});
it("offers Clerk workspace selection instead of an endless loading shell", () => {
  render(<WorkspaceShell />);
  expect(screen.getByRole("heading", { name: "Choose your workspace" })).toBeInTheDocument();
  expect(state.selectProps).toHaveBeenCalledWith(expect.objectContaining({ hidePersonal: true, afterSelectOrganizationUrl: "/chat" }));
});
it("never substitutes the user ID for a missing workspace", () => {
  state.phase = "ready";
  expect(() => render(<WorkspaceShell />)).not.toThrow();
  expect(screen.getByText("Loading…")).toBeInTheDocument();
});

it("shares one messaging owner across sidebar and dock, subscribing only to active Messages", () => {
  state.phase = "ready";
  state.organization = { id: "org_1" };
  const view = render(<WorkspaceShell />);
  const owner = screen.getByRole("region", { name: "Messaging owner" });
  expect(screen.getAllByRole("region", { name: "Messaging owner" })).toHaveLength(1);
  expect(within(owner).getByRole("navigation", { name: "Workspace" })).toBeInTheDocument();
  expect(within(owner).getByLabelText("Dock")).toHaveAttribute("data-dnd-disabled", "true");
  expect(owner).toHaveAttribute("data-subscribed", "true");
  state.activePanelId = "control:overview";
  view.rerender(<WorkspaceShell />);
  expect(owner).toHaveAttribute("data-subscribed", "false");
  state.activePanelId = "chat:__default__";
  view.rerender(<WorkspaceShell />);
  expect(owner).toHaveAttribute("data-subscribed", "true");
});

it("preserves checkout confirmation, readiness and the recovery stepper", () => {
  state.phase = "ready";
  state.organization = { id: "org_1" };
  state.chatReady = false;
  render(<WorkspaceShell />);
  expect(screen.getByText("Subscription confirmed! Your agent is being upgraded.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Recover" })).toHaveAttribute("data-ready", "false");
  fireEvent.click(screen.getByRole("button", { name: "Recover" }));
  expect(screen.getByTestId("stepper")).toHaveAttribute("data-trigger", "recovery");
});

it("keeps observing room runs while Browser is open without marking the room visible", () => {
  state.phase = "ready";
  state.organization = { id: "org_1" };
  state.activePanelId = "browser";
  state.openPanelIds = ["chat:__default__", "browser"];
  render(<WorkspaceShell />);
  const owner = screen.getByRole("region", { name: "Messaging owner" });
  expect(owner).toHaveAttribute("data-observe-runs", "true");
  expect(owner).toHaveAttribute("data-subscribed", "false");
});

it("keeps the room live and the dock headers shown while Tasks is split beside Messages", () => {
  state.phase = "ready";
  state.organization = { id: "org_1" };
  state.split = { messagesVisible: true, teamsPanelVisible: true };
  state.activePanelId = "teams-issues";
  const view = render(<WorkspaceShell />);
  const owner = screen.getByRole("region", { name: "Messaging owner" });
  expect(owner).toHaveAttribute("data-subscribed", "true");
  // Focus back in the conversation: tab bars stay, Tasks' header controls too.
  state.activePanelId = "chat:__default__";
  view.rerender(<WorkspaceShell />);
  expect(view.container.querySelector(".workspace-shell")).toHaveAttribute("data-messages-active", "false");
  state.split = null;
  view.rerender(<WorkspaceShell />);
  expect(view.container.querySelector(".workspace-shell")).toHaveAttribute("data-messages-active", "true");
});

it("opens the rail and sidebar as the phone drawer, and closes it on a pick, the backdrop or Escape", () => {
  state.phase = "ready";
  state.organization = { id: "org_1" };
  const view = render(<WorkspaceShell />);
  const shell = view.container.querySelector(".workspace-shell")!;
  const menu = screen.getByRole("button", { name: "Open menu", hidden: true });
  const main = view.container.querySelector(".workspace-main")!;
  const open = () => fireEvent.click(menu);
  expect(shell).toHaveAttribute("data-nav-open", "false");
  expect(main).not.toHaveAttribute("inert");
  open();
  expect(shell).toHaveAttribute("data-nav-open", "true");
  // Focus goes into the drawer and the covered pane is out of reach.
  expect(screen.getByRole("button", { name: "Search" })).toHaveFocus();
  expect(main).toHaveAttribute("inert");
  fireEvent.click(screen.getByRole("button", { name: "Open a room" }));
  expect(shell).toHaveAttribute("data-nav-open", "false");
  expect(main).not.toHaveAttribute("inert");
  expect(menu).toHaveFocus();
  open();
  fireEvent.click(view.container.querySelector(".workspace-nav-backdrop")!);
  expect(shell).toHaveAttribute("data-nav-open", "false");
  open();
  fireEvent.keyDown(shell, { key: "Escape" });
  expect(shell).toHaveAttribute("data-nav-open", "false");
});

it("closes the phone drawer when the window widens past the phone breakpoint", () => {
  // Wider, the backdrop is hidden: an open drawer would leave the pane inert with no way out.
  let onChange: (() => void) | undefined;
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: false, media: query,
    addEventListener: (_: string, cb: () => void) => { onChange = cb; },
    removeEventListener: () => {},
  }));
  state.phase = "ready";
  state.organization = { id: "org_1" };
  const view = render(<WorkspaceShell />);
  fireEvent.click(screen.getByRole("button", { name: "Open menu" }));
  const main = view.container.querySelector(".workspace-main")!;
  expect(main).toHaveAttribute("inert");
  act(() => onChange?.());
  expect(view.container.querySelector(".workspace-shell")).toHaveAttribute("data-nav-open", "false");
  expect(main).not.toHaveAttribute("inert");
  vi.unstubAllGlobals();
});
