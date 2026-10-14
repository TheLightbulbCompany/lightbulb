// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SWRConfig } from "swr";
import type { DockviewApi, SerializedDockview } from "dockview-react";
import type { RoomDetail, LocalSession, MessagePage } from "@lightbulb/shared";
import { MessagingWorkspaceProvider, useMessagingWorkspace } from "@/components/messaging/MessagingWorkspaceContext";
import { WorkspaceSidebar } from "../WorkspaceSidebar";
import { ChatPanel } from "../panels/ChatPanel";
import { attachDockviewApi, getWorkspaceSnapshot, openChatPanel, openChatPanelWithDraft, openPanel, openSessionPanel, openMessagePanel } from "../workspaceController";
import { parseCommand } from "../urlChannel";
import { MESSAGES_PANEL_ID } from "../panelRegistry";
import { layoutStorageKey, readLayout, writeLayout } from "../layoutStorage";

vi.mock("@/components/messaging/TrialRecapCard", () => ({ TrialRecapCard: () => null }));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ isLoaded: true, userId: "user-a", orgId: "org-a" }),
  useUser: () => ({ user: { fullName: "Alice" } }),
}));
let defaultId: string | undefined;
vi.mock("@/hooks/useAgents", () => ({ useAgents: () => ({ agents: [], defaultId }) }));
vi.mock("@/components/agents/useRosterEntries", () => ({ useRosterEntries: () => [] }));
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read: (path: string) => ({ data: path === "/agents"
  ? { agents: [{ id: "agent-a", name: "Alpha" }, { id: "agent-b", name: "Beta" }] } : { members: [] } }) }) }));
vi.mock("@/hooks/useBilling", () => ({ useBilling: () => ({ account: null, isSubscribed: true }) }));
vi.mock("@/components/settings/CompanyStripeSection", () => ({ useCompanyStripeStatus: () => ({ data: {} }) }));
vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({
  useMarketplaceDrawer: () => ({ state: {}, open: vi.fn(), setFreshlyInstalled: vi.fn() }),
}));
vi.mock("@/components/chat/HealthIndicator", () => ({ HealthIndicator: () => null }));
vi.mock("@/components/desktop/NodeStatusPill", () => ({ NodeStatusPill: () => null }));
vi.mock("@/components/desktop/DesktopUpdater", () => ({ DesktopUpdater: () => null }));
vi.mock("@/components/Logo", () => ({ Logo: () => null }));
function dm(roomId: string, agentId: string, name: string): RoomDetail {
  const joined = { joinedAt: "2026-09-10T00:00:00Z", joinedSeq: 0, mentionHandle: null };
  return {
    roomId, name, kind: "dm", roomRevision: 1, deliveryKey: `key-${roomId}`,
    lastMessageSeq: 0, lastMessageAt: null, unreadCount: 0, lastReadSeq: 0,
    updatedAt: joined.joinedAt, createdAt: joined.joinedAt,
    topic: null, starred: false, muted: false, sectionId: null, pinnedMessageIds: [],
    members: [
      { ...joined, kind: "human", id: "user-a", displayName: "Alice", role: "owner", listenMode: null },
      { ...joined, kind: "agent", id: agentId, displayName: agentId, role: "member", listenMode: "active" },
    ],
  };
}
let listed: RoomDetail[];
const nativeSession: LocalSession = {
  sessionId: "native-one", deviceId: "mac", humanUserId: "user-a", provider: "codex", providerVersion: "test",
  generation: "g1", status: "idle", activeTurnId: null, capabilities: { sendMode: "queue", steer: false, interrupt: false },
  capabilitiesVerified: true, controlUnavailableReason: null, latestEventSeq: 1,
};
const messaging = {
  listRooms: vi.fn(async () => ({ rooms: listed })),
  getRoom: vi.fn(async (id: string) => listed.find((room) => room.roomId === id)!),
  createRoom: vi.fn(),
  listMessages: vi.fn<(roomId: string) => Promise<MessagePage>>(async () => ({ messages: [], nextAfterSeq: null })),
  listWorkspaceSessions: vi.fn(async () => ({ sessions: [nativeSession] })),
  listLocalSessionEvents: vi.fn(async () => ({ events: [{ seq: 1, kind: "text", role: "assistant", text: "Synthetic native investigation history", truncated: false }], nextAfterSeq: null })),
  submitLocalSessionCommand: vi.fn(),
  markRead: vi.fn(async () => ({})),
};
const live = {
  httpReady: true, liveConnected: false, liveChannels: [] as string[], connectionRevision: 1, messaging,
  onEvent: () => () => {}, claimActiveRoomSubscription: () => () => {},
};
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => live, useOptionalLiveEvents: () => null }));

// Dockview merges parameter patches and emits them to its mounted React panel.
function dock() {
  const listeners = new Set<() => void>();
  const panels = new Map<string, ReturnType<typeof panel>>();
  let active: ReturnType<typeof panel> | undefined;
  function panel(id: string, params: Record<string, string>, title?: string) {
    const changes = new Set<(params: Record<string, string | undefined>) => void>();
    const value = { id, params, title, api: {
      id, setTitle: (next: string) => { value.title = next; },
      setActive: () => { active = value; listeners.forEach((listener) => listener()); },
      onDidParametersChange: (listener: (params: Record<string, string | undefined>) => void) => {
        changes.add(listener); return { dispose: () => changes.delete(listener) };
      },
      updateParameters: (next: Record<string, string | undefined>) => {
        for (const [key, entry] of Object.entries(next)) {
          if (entry === undefined) delete params[key]; else params[key] = entry;
        }
        changes.forEach((listener) => listener(next));
      },
    } };
    return value;
  }
  const api = {
    get panels() { return [...panels.values()]; },
    get activePanel() { return active; },
    get groups() { return [{ activePanel: active }]; },
    getPanel: (id: string) => panels.get(id),
    addPanel: (options: { id: string; params: Record<string, string>; title?: string }) => {
      const value = panel(options.id, options.params, options.title);
      panels.set(value.id, value); value.api.setActive(); return value;
    },
    onDidActivePanelChange: (listener: () => void) => {
      listeners.add(listener); return { dispose: () => listeners.delete(listener) };
    },
    onDidAddPanel: () => ({ dispose() {} }), onDidRemovePanel: () => ({ dispose() {} }),
    onDidLayoutChange: () => ({ dispose() {} }),
  };
  return api as unknown as DockviewApi;
}
function SelectedRoom() {
  return <output aria-label="Selected room">{useMessagingWorkspace().activeRoomId}</output>;
}
let detach: () => void;
function renderWorkspace(agentId = "agent-a", search?: string) {
  const api = dock();
  detach = attachDockviewApi(api, () => "agent-a");
  // openChatPanel must run regardless of `search` or MESSAGES_PANEL_ID never
  // exists for the tests below to bind ChatPanel to. A session command
  // (task-8) retargets this same Messages panel — there is no separate dock
  // panel — so the params grabbed below already reflect any session command.
  openChatPanel(agentId);
  if (search) {
    const command = parseCommand(search)!;
    openSessionPanel(command.descriptor.params.sessionId);
  }
  const panel = api.getPanel(MESSAGES_PANEL_ID)!;
  const props = { api: panel.api, params: { ...panel.params } } as React.ComponentProps<typeof ChatPanel>;
  const tree = () => <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
    <MessagingWorkspaceProvider>
      <WorkspaceSidebar chatReady onRecoveryReprovision={() => {}} />
      <ChatPanel {...props} /><SelectedRoom />
    </MessagingWorkspaceProvider>
  </SWRConfig>;
  const view = render(tree());
  return { api, rerender: () => view.rerender(tree()) };
}
beforeEach(() => {
  defaultId = "agent-a";
  listed = [dm("a-first", "agent-a", "First Alpha"), dm("a-exact", "agent-a", "Exact Alpha"),
    { ...dm("channel", "agent-a", "General"), kind: "channel" }];
  vi.clearAllMocks();
  live.liveChannels = [];
  messaging.getRoom.mockImplementation(async (id) => listed.find((room) => room.roomId === id)!);
  messaging.listMessages.mockResolvedValue({ messages: [], nextAfterSeq: null });
});
afterEach(() => { cleanup(); detach?.(); });

describe("Messages room/controller boundary", () => {
  it("opens an explicitly posted session URL from canonical room Markdown into the Messages panel", async () => {
    live.liveChannels = ["/rooms/a-first/key-a-first", "/rooms/a-exact/key-a-exact"];
    messaging.listMessages.mockImplementation(async roomId => ({ messages: [{
      messageId: "link-message", roomId, messageSeq: 1, roomRevision: 1,
      author: { kind: "human", id: "user-a", displayName: "Alice" },
      text: `[Continue native session](${window.location.origin}/chat?panel=chat&room=a-exact&session=native-one)`,
      mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: "2026-09-10T00:00:00Z", runId: null,
    }], nextAfterSeq: null }));
    const { api } = renderWorkspace();
    fireEvent.click(await screen.findByRole("link", { name: "Continue native session" }));
    // Session links retarget Messages (task-8) — there is no separate dock panel.
    await waitFor(() => expect(api.getPanel(MESSAGES_PANEL_ID)?.params).toMatchObject({ sessionId: "native-one" }));
    expect(api.getPanel(MESSAGES_PANEL_ID)?.params?.roomId).toBeUndefined();
    expect(api.panels.filter(panel => panel.id === MESSAGES_PANEL_ID)).toHaveLength(1);
    expect(messaging.submitLocalSessionCommand).not.toHaveBeenCalled();
  });
  it("openSessionPanel focuses the Messages panel and bumps sessionIntent on repeat opens, never duplicating it", async () => {
    const { api } = renderWorkspace("agent-a", "?panel=chat&room=a-exact&session=native-one");
    await waitFor(() => expect(api.getPanel(MESSAGES_PANEL_ID)?.params).toMatchObject({ sessionId: "native-one" }));
    const firstIntent = api.getPanel(MESSAGES_PANEL_ID)?.params?.sessionIntent;
    act(() => openSessionPanel("native-one"));
    expect(api.panels.filter(panel => panel.id === MESSAGES_PANEL_ID)).toHaveLength(1); // focused, not duplicated
    expect(api.getPanel(MESSAGES_PANEL_ID)?.params?.sessionIntent).not.toBe(firstIntent);
    act(() => openMessagePanel("channel"));
    await waitFor(() => expect(screen.getByLabelText("Selected room")).toHaveTextContent("channel"));
    expect(api.panels.filter(panel => panel.id === MESSAGES_PANEL_ID)).toHaveLength(1);
    expect(messaging.createRoom).not.toHaveBeenCalled();
    expect(messaging.submitLocalSessionCommand).not.toHaveBeenCalled();
  });
  it("opens the roomless panel=session permalink grammar into the Messages panel and shows the session", async () => {
    const { api } = renderWorkspace("agent-a", "?panel=session&session=native-one");
    await waitFor(() => expect(api.getPanel(MESSAGES_PANEL_ID)?.params).toMatchObject({ sessionId: "native-one" }));
    expect(await screen.findByRole("region", { name: "Native session history" })).toBeInTheDocument();
    expect(document.querySelector(".session-status-line")).toHaveTextContent("Idle");
    expect(await screen.findByText("Synthetic native investigation history")).toBeInTheDocument();
    expect(api.panels.filter(panel => panel.id === MESSAGES_PANEL_ID)).toHaveLength(1);
  });
  it("keeps a session deep link open while the cold load settles on its landing room", async () => {
    // Cold load: the restored Messages panel is still resolving its agent's DM when the URL's session command lands.
    listed = [{ ...dm("channel", "agent-a", "General"), kind: "channel" }, dm("a-first", "agent-a", "First Alpha")];
    const { api } = renderWorkspace();
    act(() => openSessionPanel("native-one"));
    await waitFor(() => expect(api.getPanel(MESSAGES_PANEL_ID)?.params).toMatchObject({ sessionId: "native-one" }));
    expect(await screen.findByRole("region", { name: "Native session history" })).toBeInTheDocument();
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    expect(screen.getByRole("region", { name: "Native session history" })).toBeInTheDocument();
    expect(await screen.findByText("Synthetic native investigation history")).toBeInTheDocument();
  });
  it("keeps the exact clicked DM, while an explicit same-agent shortcut can retarget it", async () => {
    const { api } = renderWorkspace();
    await waitFor(() => expect(screen.getByLabelText("Selected room")).toHaveTextContent("a-first"));
    fireEvent.click(await screen.findByRole("button", { name: "General Channel" }));
    await waitFor(() => expect(screen.getByLabelText("Selected room")).toHaveTextContent("channel"));
    expect(getWorkspaceSnapshot()).toMatchObject({ focusedChatAgentId: null, visibleChatAgentIds: [] });
    fireEvent.click(await screen.findByRole("button", { name: /Exact Alpha.*Direct message/ }));
    await act(async () => {});
    expect(screen.getByLabelText("Selected room")).toHaveTextContent("a-exact");
    expect(getWorkspaceSnapshot().focusedChatAgentId).toBe("agent-a");
    fireEvent.click(screen.getByRole("button", { name: /Exact Alpha.*Direct message/ }));
    await act(async () => {});
    expect(screen.getByLabelText("Selected room")).toHaveTextContent("a-exact");
    act(() => openChatPanel("agent-a"));
    await waitFor(() => expect(screen.getByLabelText("Selected room")).toHaveTextContent("a-first"));
    expect(api.panels.filter((panel) => panel.id === MESSAGES_PANEL_ID)).toHaveLength(1);
  });

  it("hands a draft to the selected same-agent DM after navigation settles", async () => {
    renderWorkspace();
    await waitFor(() => expect(screen.getByLabelText("Selected room")).toHaveTextContent("a-first"));
    fireEvent.click(await screen.findByRole("button", { name: /Exact Alpha.*Direct message/ }));
    await waitFor(() => expect(screen.getByLabelText("Selected room")).toHaveTextContent("a-exact"));

    act(() => expect(openChatPanelWithDraft("file the inbox")).toBe(true));

    await waitFor(() => expect(screen.getByLabelText("Selected room")).toHaveTextContent("a-first"));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("file the inbox"));
  });

  it("synchronizes a newly created DM after late detail hydration for workspace consumers", async () => {
    const created = dm("b-new", "agent-b", "Beta conversation");
    let hydrate!: (room: RoomDetail) => void;
    const delayed = new Promise<RoomDetail>((resolve) => { hydrate = resolve; });
    messaging.getRoom.mockImplementation(async (id) => id === created.roomId ? delayed : listed.find((room) => room.roomId === id)!);
    messaging.createRoom.mockImplementation(async () => { listed = [...listed, created]; return created; });
    const { api } = renderWorkspace();
    await waitFor(() => expect(getWorkspaceSnapshot().focusedChatAgentId).toBe("agent-a"));
    await waitFor(() => expect(screen.getByLabelText("Selected room")).toHaveTextContent("a-first"));
    fireEvent.click(await screen.findByRole("button", { name: "New conversation" }));
    fireEvent.change(screen.getByLabelText("Conversation type"), { target: { value: "dm" } });
    fireEvent.click(screen.getByLabelText("Select Beta"));
    fireEvent.click(screen.getByRole("button", { name: "Start direct message" }));
    await waitFor(() => expect(screen.getByLabelText("Selected room")).toHaveTextContent("b-new"));
    await act(async () => { hydrate(created); });
    await waitFor(() => expect(getWorkspaceSnapshot()).toMatchObject({
      focusedChatAgentId: "agent-b", lastFocusedChatAgentId: "agent-b", visibleChatAgentIds: ["agent-b"],
    }));
    act(() => openPanel("file-viewer"));
    expect(getWorkspaceSnapshot().lastFocusedChatAgentId).toBe("agent-b");
    expect(getWorkspaceSnapshot().activePanelId).toBe("file-viewer");
    // The resolved marker is private runtime state, never a persisted selection command.
    expect(api.getPanel(MESSAGES_PANEL_ID)?.params?.agentSelection).toBe("resolved");
    writeLayout("integration", {
      grid: { root: { type: "branch", data: [] }, width: 800, height: 600, orientation: "HORIZONTAL" as SerializedDockview["grid"]["orientation"] },
      panels: { [MESSAGES_PANEL_ID]: { id: MESSAGES_PANEL_ID, contentComponent: "chat", params: api.getPanel(MESSAGES_PANEL_ID)!.params } },
    });
    expect(readLayout("integration")?.panels[MESSAGES_PANEL_ID].params).toEqual({ agentId: "agent-b" });
    expect(JSON.parse(localStorage.getItem(layoutStorageKey("integration"))!).layout.panels[MESSAGES_PANEL_ID].params)
      .toEqual({ agentId: "agent-b" });
  });

  it("keeps an unresolved default-agent command until the roster arrives", async () => {
    defaultId = undefined;
    listed = [listed[2], listed[0], listed[1]];
    const { api, rerender } = renderWorkspace("__default__");
    await waitFor(() => expect(screen.getByLabelText("Selected room")).toHaveTextContent("channel"));
    expect(api.getPanel(MESSAGES_PANEL_ID)?.params?.agentId).toBe("__default__");
    defaultId = "agent-a";
    rerender();
    await waitFor(() => expect(screen.getByLabelText("Selected room")).toHaveTextContent("a-first"));
  });
});
