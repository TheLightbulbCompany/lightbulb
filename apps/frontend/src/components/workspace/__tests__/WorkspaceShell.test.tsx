// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { WorkspaceShell } from "../WorkspaceShell";
import { _resetControllerForTests, activateMessagesPanel, getWorkspaceSnapshot, closePane, openPanel, useThreadPane } from "../workspaceController";
import { PANE_DEFAULT_WIDTH, paneWidthKey } from "../RightPane";

const state = vi.hoisted(() => ({
  phase: "selecting", selectProps: vi.fn(), organization: null as { id: string } | null, chatReady: true, superAgent: false,
}));
vi.mock("@/hooks/useLiveEvents", async (importOriginal) => ({ ...await importOriginal<object>(), useSuperAgent: () => state.superAgent }));
vi.mock("@/hooks/useOnboardingGate", () => ({ useOnboardingGate: () => state.phase }));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ isSignedIn: true, userId: "user_1", orgId: "org_1" }),
  useOrganization: () => ({ organization: state.organization }),
  OrganizationList: (props: unknown) => { state.selectProps(props); return null; },
}));
// An owner-aware child must never mount without an active workspace.
vi.mock("@/hooks/useAgents", () => ({ useAgents: () => {
  if (!state.organization) throw new Error("Owner data requested without a workspace");
  return { agents: [], defaultId: undefined, isSettled: true };
} }));
vi.mock("next/navigation", () => ({ useSearchParams: () => null }));
// The real controller; the panels are stand-ins (their own tests cover them).
vi.mock("../panels/ChatPanel", () => ({
  ChatPanel: ({ params }: { params: Record<string, string> }) => (
    <section aria-label="Conversation" data-agent={params.agentId} data-room={params.roomId}><textarea aria-label="Message" /></section>
  ),
}));
vi.mock("../panelComponents", () => ({
  PANEL_COMPONENTS: new Proxy({}, {
    get: (_target, kind: string) => function Stub({ id }: { id: string }) { return <div data-testid={`panel-${kind}`}>{id}</div>; },
  }),
}));
vi.mock("@/components/messaging/MessagingWorkspaceContext", () => ({
  MessagingWorkspaceProvider: ({ children, subscribeToRoom, observeRuns }: { children: ReactNode; subscribeToRoom: boolean; observeRuns?: boolean }) => (
    <section aria-label="Messaging owner" data-subscribed={subscribeToRoom} data-observe-runs={observeRuns}>{children}</section>
  ),
}));
vi.mock("../WorkspaceSidebar", () => ({
  WorkspaceSidebar: ({ onRecoveryReprovision, chatReady, onNavigate, id, dialog }: { onRecoveryReprovision: () => void; chatReady: boolean; onNavigate: () => void; id?: string; dialog?: boolean }) => (
    <nav className="cream-sidebar" id={id} {...(dialog ? { role: "dialog", "aria-modal": true, "aria-label": "Conversations" } : { "aria-label": "Workspace" })}>
      <button onClick={onRecoveryReprovision} data-ready={chatReady}>Recover</button>
      <button onClick={onNavigate}>Open a room</button>
    </nav>
  ),
}));
vi.mock("../ChatHistorySync", () => ({ ChatHistorySync: () => null }));
vi.mock("../InvitePeopleDialog", () => ({ InvitePeopleDialog: () => null }));
vi.mock("../WorkspaceHeader", () => ({ WorkspaceHeader: () => <nav className="workspace-rail"><button disabled>Back</button><button>Search</button></nav> }));
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
vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({ useMarketplaceDrawer: () => ({ open: vi.fn() }) }));
vi.mock("@/components/chat/shell/useUserSyncOnce", () => ({ useUserSyncOnce: () => {} }));
vi.mock("@/components/chat/shell/useCheckoutReturn", () => ({ useCheckoutReturn: () => ({ showSubscriptionSuccess: true }) }));
vi.mock("@/components/chat/shell/useChatReady", () => ({ useChatReady: () => ({ chatReady: state.chatReady }) }));
beforeEach(() => {
  state.phase = "selecting";
  state.organization = null;
  state.chatReady = true;
  _resetControllerForTests();
  window.localStorage.clear();
  window.history.replaceState(null, "", "/chat");
  vi.clearAllMocks();
});
function renderReady() {
  state.phase = "ready";
  state.organization = { id: "org_1" };
  return render(<WorkspaceShell />);
}
const shellOf = (view: ReturnType<typeof render>) => view.container.querySelector(".workspace-shell")!;
const paneOf = () => screen.queryByRole("region", { name: /Files|Browser|Task/ });

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

it("shares one messaging owner across sidebar and conversation, subscribing only while the conversation shows", () => {
  renderReady();
  const owner = screen.getByRole("region", { name: "Messaging owner" });
  expect(screen.getAllByRole("region", { name: "Messaging owner" })).toHaveLength(1);
  expect(within(owner).getByRole("navigation", { name: "Workspace" })).toBeInTheDocument();
  expect(owner).toHaveAttribute("data-subscribed", "true");
  act(() => openPanel("activity"));
  expect(owner).toHaveAttribute("data-subscribed", "false");
  act(() => activateMessagesPanel());
  expect(owner).toHaveAttribute("data-subscribed", "true");
});

it("keeps the room subscribed while its thread is in the pane from another place", () => {
  // The room's side column claims the pane, as RoomPanel does.
  function ThreadColumn({ open }: { open: boolean }) { useThreadPane(open, () => {}, "Thread · Launch"); return null; }
  const view = renderReady();
  const owner = screen.getByRole("region", { name: "Messaging owner" });
  const thread = render(<ThreadColumn open />);
  act(() => openPanel("teams-issues"));
  expect(owner).toHaveAttribute("data-subscribed", "true");
  act(() => closePane());
  expect(owner).toHaveAttribute("data-subscribed", "false");
  thread.unmount();
  view.unmount();
});

it("preserves checkout confirmation, readiness and the recovery stepper", () => {
  state.chatReady = false;
  renderReady();
  expect(screen.getByText("Subscription confirmed! Your agent is being upgraded.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Recover" })).toHaveAttribute("data-ready", "false");
  fireEvent.click(screen.getByRole("button", { name: "Recover" }));
  expect(screen.getByTestId("stepper")).toHaveAttribute("data-trigger", "recovery");
});

it("observes room runs while the Browser is in the pane, whatever the place", () => {
  renderReady();
  const owner = screen.getByRole("region", { name: "Messaging owner" });
  expect(owner).toHaveAttribute("data-observe-runs", "false");
  act(() => openPanel("browser"));
  expect(owner).toHaveAttribute("data-observe-runs", "true");
  act(() => openPanel("activity"));
  expect(owner).toHaveAttribute("data-observe-runs", "true");
  expect(owner).toHaveAttribute("data-subscribed", "false");
});

it("routes each place to one main view; only Chat has the sidebar", () => {
  const view = renderReady();
  const conversation = screen.getByRole("region", { name: "Conversation", hidden: true });
  expect(conversation.closest(".workspace-view")).not.toHaveAttribute("hidden");
  expect(shellOf(view)).toHaveAttribute("data-sidebar", "true");
  for (const [key, testId] of [["activity", "panel-activity"], ["teams-issues", "panel-teams"], ["teams-artifacts", "panel-teams"]] as const) {
    act(() => openPanel(key));
    expect(screen.getByTestId(testId)).toBeInTheDocument();
    expect(conversation.closest(".workspace-view")).toHaveAttribute("hidden");
    expect(shellOf(view)).toHaveAttribute("data-sidebar", "false");
  }
  act(() => activateMessagesPanel());
  expect(screen.queryByTestId("panel-teams")).toBeNull();
  expect(conversation.closest(".workspace-view")).not.toHaveAttribute("hidden");
  expect(screen.getAllByRole("region", { name: "Conversation", hidden: true })).toHaveLength(1);
});

it("opens one thing in the pane, closes it, and remembers the width per person", () => {
  renderReady();
  act(() => openPanel("file-viewer"));
  expect(paneOf()).toHaveStyle({ width: `${PANE_DEFAULT_WIDTH}px` });
  expect(screen.getByTestId("panel-file-viewer")).toBeInTheDocument();
  // Keyboard resize from the left edge: wider, and remembered.
  fireEvent.keyDown(screen.getByRole("separator", { name: "Resize the pane" }), { key: "ArrowLeft" });
  const stored = window.localStorage.getItem(paneWidthKey("user_1"));
  expect(Number(stored)).toBeGreaterThanOrEqual(320);
  act(() => openPanel("teams-issue-detail", { detailId: "ADM-1" }));
  expect(screen.queryByTestId("panel-file-viewer")).toBeNull(); // replaced, one at a time
  expect(paneOf()).toHaveStyle({ width: `${stored}px` });
  fireEvent.click(screen.getByRole("button", { name: "Close pane" }));
  expect(paneOf()).toBeNull();
});

it("the resize separator reports its value, and a cancelled drag still keeps its width", () => {
  renderReady();
  act(() => openPanel("file-viewer"));
  const separator = screen.getByRole("separator", { name: "Resize the pane" });
  expect(separator).toHaveAttribute("aria-orientation", "vertical");
  expect(separator).toHaveAttribute("aria-valuemin", "320");
  expect(separator).toHaveAttribute("aria-valuenow");
  expect(separator).toHaveAttribute("aria-valuemax");
  fireEvent.pointerDown(separator, { pointerId: 1 });
  fireEvent(separator, new Event("pointercancel"));
  expect(Number(window.localStorage.getItem(paneWidthKey("user_1")))).toBeGreaterThanOrEqual(320);
});

it("opens the Browser at half the width, and still opens when storage throws", () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
  renderReady();
  act(() => openPanel("browser"));
  expect(screen.getByRole("region", { name: "Browser" })).toHaveStyle({ width: "50%" });
  act(() => openPanel("file-viewer"));
  expect(screen.getByRole("region", { name: "Files" })).toHaveStyle({ width: `${PANE_DEFAULT_WIDTH}px` });
  vi.restoreAllMocks();
});

it("full screen: the pane takes the screen beside a chat column with its composer; collapse restores", () => {
  const view = renderReady();
  act(() => openPanel("activity"));
  act(() => openPanel("browser"));
  fireEvent.click(screen.getByRole("button", { name: "Full screen" }));
  const body = view.container.querySelector(".workspace-body")!;
  expect(body).toHaveAttribute("data-full");
  // From a non-Chat place the column shows the conversation (the CEO DM by default).
  const column = screen.getByRole("region", { name: "Conversation" });
  expect(column.closest(".workspace-view")).not.toHaveAttribute("hidden");
  expect(within(column).getByRole("textbox", { name: "Message" })).toBeInTheDocument();
  expect(screen.getByTestId("panel-activity").closest(".workspace-view")).toHaveAttribute("hidden");
  expect(shellOf(view)).toHaveAttribute("data-sidebar", "false");
  expect(screen.getByRole("region", { name: "Browser" }).style.width).toBe("");
  fireEvent.click(screen.getByRole("button", { name: "Exit full screen" }));
  expect(body).not.toHaveAttribute("data-full");
  expect(screen.getByTestId("panel-activity").closest(".workspace-view")).not.toHaveAttribute("hidden");
  expect(screen.getByRole("region", { name: "Conversation", hidden: true }).closest(".workspace-view")).toHaveAttribute("hidden");
});

it("applies the URL's command on mount: a retired alias opens Activity, a detail opens the pane", () => {
  window.history.replaceState(null, "", "/chat?panel=teams-inbox");
  const view = renderReady();
  expect(screen.getByTestId("panel-activity")).toBeInTheDocument();
  view.unmount();
  _resetControllerForTests();
  window.history.replaceState(null, "", "/chat?panel=teams-issue-detail&detailId=ADM-9");
  renderReady();
  expect(screen.getByTestId("panel-teams")).toHaveTextContent("teams-issue-detail:ADM-9");
  expect(screen.getByRole("region", { name: "Task" })).toBeInTheDocument();
});

it("restores the room and the Browser beside it from ?panel=chat&room=…&pane=browser on reload", () => {
  window.history.replaceState(null, "", "/chat?panel=chat&room=r-launch&pane=browser");
  renderReady();
  expect(screen.getByRole("region", { name: "Conversation" })).toHaveAttribute("data-room", "r-launch");
  expect(screen.getByRole("region", { name: "Browser" })).toBeInTheDocument();
});

it("opens Canvas on app_1 from ?panel=canvas&app=app_1", () => {
  window.history.replaceState(null, "", "/chat?panel=canvas&app=app_1");
  renderReady();
  const { main } = getWorkspaceSnapshot();
  expect(main).toMatchObject({ component: "canvas", params: { app: "app_1" } });
  expect(screen.getByTestId("panel-canvas").closest(".workspace-view")).not.toHaveAttribute("hidden");
});

it("restores both the place and the pane from a two-slot URL on reload", () => {
  window.history.replaceState(null, "", "/chat?panel=teams-artifacts&pane=browser");
  renderReady();
  expect(screen.getByTestId("panel-teams")).toHaveTextContent("teams-artifacts");
  expect(screen.getByRole("region", { name: "Browser" })).toBeInTheDocument();
});

it("opens the rail and sidebar as the phone drawer, and closes it on a pick, the backdrop or Escape", () => {
  const view = renderReady();
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

it("closes the phone drawer when navigation moves the place or the pane, whatever control caused it", () => {
  const view = renderReady();
  const shell = shellOf(view);
  fireEvent.click(screen.getByRole("button", { name: "Open menu", hidden: true }));
  expect(shell).toHaveAttribute("data-nav-open", "true");
  act(() => openPanel("teams-issues"));
  expect(shell).toHaveAttribute("data-nav-open", "false");
  fireEvent.click(screen.getByRole("button", { name: "Open menu", hidden: true }));
  act(() => openPanel("file-viewer"));
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
  const view = renderReady();
  fireEvent.click(screen.getByRole("button", { name: "Open menu" }));
  const main = view.container.querySelector(".workspace-main")!;
  expect(main).toHaveAttribute("inert");
  act(() => onChange?.());
  expect(view.container.querySelector(".workspace-shell")).toHaveAttribute("data-nav-open", "false");
  expect(main).not.toHaveAttribute("inert");
  vi.unstubAllGlobals();
});

const stubMedia = (matches: (query: string) => boolean) => {
  const listeners = new Map<string, () => void>();
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: matches(query), media: query,
    addEventListener: (_: string, cb: () => void) => { listeners.set(query, cb); },
    removeEventListener: () => {},
  }));
  return listeners;
};

it("super-agent, narrow window (600-999 px): the drawer is a modal sidebar; focus starts and stays in it, the rail is inert, it closes past 1000 px (W8-2)", () => {
  state.superAgent = true;
  const listeners = stubMedia((q) => q === "(min-width: 600px)");
  try {
    const view = renderReady();
    const shell = shellOf(view);
    expect(shell).toHaveAttribute("data-super-agent", "true");
    const menu = screen.getByRole("button", { name: "Open menu" });
    expect(menu).toHaveAttribute("aria-controls", "workspace-sidebar");
    fireEvent.click(menu);
    const dialog = screen.getByRole("dialog", { name: "Conversations" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAttribute("id", "workspace-sidebar");
    expect(screen.getByRole("button", { name: "Recover" })).toHaveFocus();
    expect(view.container.querySelector(".workspace-rail")).toHaveAttribute("inert");
    // Tab from the last stop wraps to the first; Shift+Tab from the first to the last.
    screen.getByRole("button", { name: "Open a room" }).focus();
    fireEvent.keyDown(shell, { key: "Tab" });
    expect(screen.getByRole("button", { name: "Recover" })).toHaveFocus();
    fireEvent.keyDown(shell, { key: "Tab", shiftKey: true });
    expect(screen.getByRole("button", { name: "Open a room" })).toHaveFocus();
    act(() => listeners.get("(min-width: 1000px)")?.());
    expect(shell).toHaveAttribute("data-nav-open", "false");
    expect(view.container.querySelector(".workspace-rail")).not.toHaveAttribute("inert");
  } finally { state.superAgent = false; vi.unstubAllGlobals(); }
});

it("without the super-agent the drawer is the phone one: no modal sidebar, rail reachable, it closes past 600 px", () => {
  const listeners = stubMedia((q) => q === "(min-width: 600px)");
  try {
    const view = renderReady();
    expect(shellOf(view)).not.toHaveAttribute("data-super-agent");
    fireEvent.click(screen.getByRole("button", { name: "Open menu" }));
    expect(screen.queryByRole("dialog", { name: "Conversations" })).toBeNull();
    expect(view.container.querySelector(".workspace-rail")).not.toHaveAttribute("inert");
    act(() => listeners.get("(min-width: 600px)")?.());
    expect(shellOf(view)).toHaveAttribute("data-nav-open", "false");
  } finally { vi.unstubAllGlobals(); }
});

it("a pane that is a sheet is a modal dialog: what it covers is inert and Escape closes it", () => {
  state.superAgent = true;
  stubMedia((q) => q === "(max-width: 735px)");
  try {
    const view = renderReady();
    act(() => openPanel("file-viewer"));
    const pane = view.container.querySelector(".workspace-pane")!;
    expect(pane).toHaveAttribute("role", "dialog");
    expect(pane).toHaveAttribute("aria-modal", "true");
    const covered = [...pane.parentElement!.children].filter((c) => c !== pane);
    expect(covered.length).toBeGreaterThan(0);
    covered.forEach((c) => expect(c).toHaveAttribute("inert"));
    fireEvent.keyDown(pane, { key: "Escape" });
    expect(view.container.querySelector(".workspace-pane")).toBeNull();
    covered.forEach((c) => expect(c).not.toHaveAttribute("inert"));
  } finally { state.superAgent = false; vi.unstubAllGlobals(); }
});
