import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useCallback, useEffect, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, EMPTY_ROOM_SUBAGENTS, resolveTypedMentions, type CanonicalRoomMessage, type RoomDetail } from "@lightbulb/shared";
import type { RoomDirectoryProps } from "../RoomDirectory";
import type { RoomPanelProps } from "../RoomPanel";
import { RoomComposer } from "../RoomComposer";
import { draftKey } from "../drafts";
import { requestFocusComposer, _resetFocusComposerRequestForTests } from "@/lib/focusComposerRequest";
import type { LiveRoomRun } from "@lightbulb/shared";

const mocks = vi.hoisted(() => ({ rooms: vi.fn(), agents: vi.fn(), roster: vi.fn(), orgId: "org", capture: vi.fn(), waitingPref: "banner" }));
vi.mock("@/components/settings/SessionNotificationsCard", async (importOriginal) => ({ ...(await importOriginal<object>()),
  useSessionNotificationPrefs: () => ({ prefs: { waiting: mocks.waitingPref, finished: "banner" }, loaded: true, mutate: vi.fn() }) }));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ userId: "owner", orgId: mocks.orgId }), useUser: () => ({ user: { fullName: "Olive Owner" } }) }));
vi.mock("@/lib/analytics", async (importOriginal) => ({ ...(await importOriginal<object>()), capture: mocks.capture }));
vi.mock("@/hooks/useRooms", () => ({ useRooms: mocks.rooms }));
type NotificationProps = { roomVisible: boolean; onOpen: (roomId: string, threadId: string | null) => void };
let notifications: NotificationProps;
vi.mock("@/components/notifications/MessageNotifications", () => ({ MessageNotifications: (props: NotificationProps) => {
  notifications = props;
  return null;
} }));
vi.mock("@/hooks/useAgents", () => ({ useAgents: mocks.agents }));
vi.mock("@/components/agents/useRosterEntries", () => ({ useRosterEntries: mocks.roster }));
vi.mock("@/components/chat/shell/useChatReady", () => ({ useChatReady: () => ({ chatReady: true, setupStarting: false }) }));
const teams = vi.hoisted(() => ({ agentsLoading: false, superAgent: false }));
vi.mock("@/hooks/useLiveEvents", async (importOriginal) => ({ ...(await importOriginal<object>()), useSuperAgent: () => teams.superAgent }));
// The tab title reads needsYouCount under the super-agent (its own tests: HomeWork, WorkspaceHeader).
vi.mock("@/components/tasks/useRoomThreads", async (importOriginal) => ({ ...(await importOriginal<object>()), useNeedsYouCount: () => 0 }));
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({
  read: (path: string) => ({ data: path === "/agents"
    ? (teams.agentsLoading ? undefined : { agents: [{ id: "agent-friday", name: "Friday" }, { id: "agent-atlas", name: "Atlas", role: "ceo", origin: "Starter" }] })
    : { members: [{ userId: "owner", name: "Owner" }, { userId: "colleague", name: "Colleague" }] } }),
}) }));
vi.mock("@/components/workspace/workspaceController", () => ({ openPanel: vi.fn(), activateMessagesPanel: vi.fn(), openInviteDialog: vi.fn(), getWorkspaceSnapshot: () => ({ activePanelId: null }), useWorkspaceState: () => ({ place: "chat", main: null }) }));
let directory: RoomDirectoryProps;
let panel: RoomPanelProps;
let renderComposer = false;
vi.mock("../RoomDirectory", () => ({ RoomDirectory: (props: RoomDirectoryProps) => {
  directory = props;
  return <nav aria-label="Chats" />;
} }));
const editLast = vi.fn();
vi.mock("../RoomPanel", () => ({ RoomPanel: (props: RoomPanelProps) => {
  panel = props;
  return <section aria-label="Messages">{renderComposer && <RoomComposer {...props.composer} onEditLast={editLast} />}</section>;
} }));

import { MessagingWorkspaceProvider, useMessagingWorkspace, type MessagingWorkspaceValue } from "../MessagingWorkspaceContext";
import { MessagingConversation, MessagingDirectory } from "../MessagingWorkspace";

let workspaceValue: MessagingWorkspaceValue;
function CaptureContext() {
  const value = useMessagingWorkspace();
  // Assigning the module-level capture in an effect (not during render) keeps
  // this component pure per react-hooks/globals.
  useEffect(() => { workspaceValue = value; });
  return null;
}

const room: RoomDetail = {
  roomId: "room-1", kind: "dm", name: null, roomRevision: 1,
  deliveryKey: "delivery", lastMessageSeq: 0, lastMessageAt: null,
  unreadCount: 0, lastReadSeq: 0, createdAt: "2026-09-10T00:00:00Z", updatedAt: "2026-09-10T00:00:00Z",
  topic: null, starred: false, muted: false, sectionId: null, pinnedMessageIds: [],
  members: [
    { kind: "human", id: "owner", displayName: "Owner", mentionHandle: null, role: "owner", listenMode: null, joinedAt: "2026-09-10T00:00:00Z", joinedSeq: 0 },
    { kind: "agent", id: "agent-friday", displayName: "Friday", mentionHandle: "friday", role: "member", listenMode: "mentions", joinedAt: "2026-09-10T00:00:00Z", joinedSeq: 0 },
  ],
};
const state = {
  resourceSource: { client: { listLocalSessionEvents: vi.fn(), submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn(), listWorkspaceSessions: vi.fn(async () => ({ sessions: [] as never[] })),
    // A session missing from the settled list is fetched directly (SessionFocus's fallback); default to a genuine 404.
    getLocalSession: vi.fn(async () => { throw new ApiError(404, null); }),
    getRoom: vi.fn(async () => room) }, onEvent: () => () => {}, connectionRevision: 1 },
  rooms: [room], roomsError: undefined as Error | undefined, roomsLoading: false,
  directoryError: undefined as Error | undefined, directoryLoading: false,
  activeRoomId: room.roomId as string | null, activeRoom: room,
  roomError: undefined, roomLoading: false, accessRemoved: false,
  messages: [] as CanonicalRoomMessage[], optimisticMessages: [] as Array<{ clientMessageId: string; roomId: string; text: string; mentions: never[]; replyToMessageId: null; threadRootMessageId: string | null; createdAt: string }>, typingMembers: [], runs: [] as LiveRoomRun[],
  historyLoading: false, historyComplete: false, historyError: false, historyErrorMessage: null as string | null,
  loadHistory: vi.fn(), selectRoomError: null as string | null,
  liveConnected: true, httpReady: true, selectRoom: vi.fn(), selectDm: vi.fn(), selectAgentDm: vi.fn(),
  directoryRoomDetails: new Map([[room.roomId, room]]),
  refreshRooms: vi.fn(), refreshRoom: vi.fn(), createRoom: vi.fn(), updateRoom: vi.fn(),
  upsertMember: vi.fn(), removeMember: vi.fn(), deleteRoom: vi.fn(), sendMessage: vi.fn(), reconcileSend: vi.fn(),
  sendState: () => "idle", setTyping: vi.fn(), abortRun: vi.fn(), steerRun: vi.fn(), subagents: EMPTY_ROOM_SUBAGENTS, refreshSubagents: vi.fn(), subagentsPollVisible: vi.fn(), steerSubagent: vi.fn(), stopSubagent: vi.fn(),
};
beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  state.rooms = [room];
  state.activeRoomId = room.roomId;
  state.activeRoom = room;
  state.roomsError = undefined;
  state.optimisticMessages = [];
  state.messages = [];
  state.directoryError = undefined;
  state.directoryLoading = false;
  state.accessRemoved = false;
  state.runs = [];
  state.selectDm.mockResolvedValue(null);
  // Never resolves by default: the provider's mount-time sessions fetch would
  // otherwise settle-then-setState on a microtask this file's ~30 synchronous
  // `it(() => {...})` bodies never yield to, landing outside any act() scope
  // for a value (empty list, unchanged) none of them assert on anyway. The one
  // test that cares (below) installs its own resolving mock before rendering.
  state.resourceSource.client.listWorkspaceSessions.mockImplementation(() => new Promise(() => {}));
  renderComposer = false;
  _resetFocusComposerRequestForTests();
  mocks.rooms.mockReturnValue(state);
  mocks.agents.mockReturnValue({ agents: [{ id: "agent-friday", name: "Friday" }] });
  mocks.roster.mockReturnValue([{ agent: { id: "agent-friday" }, state: "running" }]);
  state.selectRoomError = null;
  state.historyError = false;
  state.historyErrorMessage = null;
  state.historyComplete = false;
  state.historyLoading = false;
});
// Belt-and-suspenders for the one test below that does let its sessions fetch
// resolve: flush inside act(), before cleanup unmounts the tree.
afterEach(async () => {
  await act(async () => {});
  cleanup();
  localStorage.clear();
});

function compose(onOpenRoom?: (room?: import("../types").DirectoryRoom) => void) {
  return render(<MessagingWorkspaceProvider initialAgentId="agent-friday" subscribeToRoom>
    <CaptureContext />
    <MessagingDirectory onOpenRoom={onOpenRoom} />
    <MessagingConversation />
  </MessagingWorkspaceProvider>);
}

it("feeds the directory and conversation from one selected-room owner", () => {
  const view = compose();
  // Every render (the first room-list load re-renders once to latch the
  // landing) asks the one provider-owned hook the same thing.
  expect(mocks.rooms.mock.calls).toEqual(mocks.rooms.mock.calls.map(() => ["agent-friday", true, true, true, true]));
  expect(state.selectAgentDm).not.toHaveBeenCalled();
  expect(directory.currentRoomId).toBe(panel.roomId);
  expect(screen.getByRole("navigation", { name: "Chats" })).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Messages" })).toBeInTheDocument();
  view.unmount();
});

it("names the starter CEO to the directory by role, never by display name", () => {
  const view = compose();
  expect(directory.ceoAgentId).toBe("agent-atlas");
  view.unmount();
});

it("records room_opened once per room and composer_typing_started once per room per mount", () => {
  mocks.capture.mockClear();
  const view = compose();
  const events = (name: string) => mocks.capture.mock.calls.filter(([event]) => event === name);
  // Landing in the room (an explicit pick or the fresh-workspace auto-open)
  // is the trace that a person SAW a room; kind only, never the room's name.
  expect(events("room_opened")).toEqual([["room_opened", { room_kind: "dm", workspace_id: "org" }]]);
  // Typing bursts in one room count once: the question is "did they start to
  // type", not how many times the indicator flickered.
  const typing = panel.composer.onTypingChange;
  if (!typing) throw new Error("composer exposes no onTypingChange");
  act(() => { typing(true); });
  act(() => { typing(false); });
  act(() => { typing(true); });
  expect(events("composer_typing_started")).toEqual([
    ["composer_typing_started", { room_kind: "dm", workspace_id: "org" }],
  ]);
  view.unmount();
});

const firstChatMessage: CanonicalRoomMessage = {
  messageId: "first-message",
  roomId: room.roomId,
  messageSeq: 1,
  roomRevision: 2,
  author: { kind: "human", id: "owner", displayName: "Owner" },
  text: "private customer content",
  mentions: [],
  replyToMessageId: null,
  threadRootMessageId: null,
  createdAt: "2026-09-10T00:01:00Z",
  runId: null,
};

const firstChatRun = (runState: LiveRoomRun["state"]): LiveRoomRun => ({
  roomId: room.roomId,
  agentId: "agent-friday",
  runId: "first-run",
  triggerMessageId: firstChatMessage.messageId,
  text: runState === "completed" ? "A useful answer" : "",
  thinking: "",
  frames: [],
  state: runState,
});

function makeWorkspaceFresh() {
  const general = { ...room, kind: "channel" as const, isDefault: true };
  state.rooms = [general];
  state.activeRoom = general;
  state.messages = [firstChatMessage];
}

it("emits one aggregate chat_completed event when the browser observes the first real chat complete", async () => {
  makeWorkspaceFresh();
  state.runs = [firstChatRun("running")];
  const tree = () => (<MessagingWorkspaceProvider initialAgentId="agent-friday" subscribeToRoom>
    <CaptureContext /><MessagingDirectory /><MessagingConversation />
  </MessagingWorkspaceProvider>);
  const view = render(tree());
  expect(mocks.capture.mock.calls.filter(([event]) => event === "chat_completed")).toEqual([]);

  state.runs = [firstChatRun("completed")];
  view.rerender(tree());
  await waitFor(() => expect(mocks.capture).toHaveBeenCalledWith("chat_completed", { workspace_id: "org" }));
  expect(mocks.capture.mock.calls.filter(([event]) => event === "chat_completed")).toHaveLength(1);

  // The same completed run, a rerender, and a full remount are all replays.
  view.rerender(tree());
  view.unmount();
  render(tree());
  expect(mocks.capture.mock.calls.filter(([event]) => event === "chat_completed")).toHaveLength(1);
});

it.each(["failed", "aborted"] as const)("does not emit chat_completed when the first chat run ends %s", async (outcome) => {
  makeWorkspaceFresh();
  state.runs = [firstChatRun("running")];
  const tree = () => (<MessagingWorkspaceProvider initialAgentId="agent-friday" subscribeToRoom>
    <CaptureContext /><MessagingDirectory /><MessagingConversation />
  </MessagingWorkspaceProvider>);
  const view = render(tree());
  state.runs = [firstChatRun(outcome)];
  view.rerender(tree());
  await act(async () => {});
  expect(mocks.capture.mock.calls.filter(([event]) => event === "chat_completed")).toEqual([]);
});

it("does not emit chat_completed for a completed run that the browser never observed running", async () => {
  makeWorkspaceFresh();
  state.runs = [firstChatRun("completed")];
  render(<MessagingWorkspaceProvider initialAgentId="agent-friday" subscribeToRoom>
    <CaptureContext /><MessagingDirectory /><MessagingConversation />
  </MessagingWorkspaceProvider>);
  await act(async () => {});
  expect(mocks.capture.mock.calls.filter(([event]) => event === "chat_completed")).toEqual([]);
});

it("opens the room a failed-run toast names", async () => {
  state.resourceSource.client.getRoom.mockResolvedValueOnce({ ...room, roomId: "room-9" });
  const view = compose();
  act(() => { window.dispatchEvent(new CustomEvent("openRoom", { detail: { roomId: "room-9", threadId: null } })); });
  await waitFor(() => expect(state.selectRoom).toHaveBeenCalledWith("room-9"));
  view.unmount();
});

it("opens a thread on its root when the openRoom event names one (Activity's Open thread)", () => {
  const view = compose();
  act(() => { window.dispatchEvent(new CustomEvent("openRoom", { detail: { roomId: room.roomId, threadId: "root-1" } })); });
  expect(state.selectRoom).toHaveBeenCalledWith(room.roomId);
  expect(panel.selectedThreadId).toBe("root-1");
  view.unmount();
});

it("routes message authors through the existing participant DM action", async () => {
  compose();
  await act(async () => panel.onSelectParticipant?.(panel.participants[1]));
  expect(state.selectDm).toHaveBeenCalledWith(panel.participants[1]);
});

it("surfaces a failed DM resolve as a dismissible alert and passes the history reason through", () => {
  // Fresh element per render: rerendering the identical object bails out and
  // would never re-read the mocked hook.
  const tree = () => (<MessagingWorkspaceProvider initialAgentId="agent-friday" subscribeToRoom>
    <CaptureContext />
    <MessagingDirectory onOpenRoom={undefined} />
    <MessagingConversation />
  </MessagingWorkspaceProvider>);
  const view = render(tree());
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  state.selectRoomError = "Conversation unavailable";
  state.historyError = true;
  state.historyErrorMessage = "History is temporarily unavailable";
  view.rerender(tree());
  expect(screen.getByRole("alert")).toHaveTextContent("Conversation unavailable");
  expect(panel.historyError).toBe(true);
  expect(panel.historyErrorMessage).toBe("History is temporarily unavailable");
  fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("returns DM failures to the profile so it can offer retry", async () => {
  state.selectDm.mockRejectedValueOnce(new Error("Conversation unavailable"));
  compose();
  await expect(panel.onSelectParticipant?.(panel.participants[1])).rejects.toThrow("Conversation unavailable");
});

it("renders OpenClaw string tool progress without crashing", () => {
  state.runs = [{
    roomId: room.roomId,
    agentId: "agent-friday",
    runId: "run-1",
    text: "",
    thinking: "",
    frames: [{
      type: "tool_progress",
      tool: "exec",
      toolCallId: "tool-1",
      partialResult: "streamed output",
      runId: "run-1",
    } as unknown as LiveRoomRun["frames"][number]],
    state: "running",
  }];

  compose();

  expect(panel.runs?.[0]?.tools[0]?.progressText).toBe("streamed output");
});

it("enriches room participants with roster activity while keeping the full creation picker", () => {
  compose();
  expect(directory.rooms[0].participants).toEqual([expect.objectContaining({ id: "agent-friday", agentActivity: "running" })]);
  expect(panel.participants[1]).toMatchObject({ id: "agent-friday", agentActivity: "running" });
  expect(panel.participants[1]).not.toHaveProperty("agentState");
  expect(panel.participants[0]).not.toHaveProperty("agentActivity");
  expect(panel.participants[0]).not.toHaveProperty("agentState");
  expect(directory.participants.map((member) => member.id)).toEqual(["colleague", "agent-friday", "agent-atlas"]);
});

it("offers a shared teammate session whose owner is in the room, without a control grant", async () => {
  // A chip is a reference (#3623): no send/control predicate gates the picker.
  state.resourceSource.client.listWorkspaceSessions.mockResolvedValue({ sessions: [{ ...(liveSession("s1", "colleague") as object), shared: true, controlUnavailableReason: "not_owner" } as never] });
  state.activeRoom = { ...room, members: [...room.members, { kind: "human", id: "colleague", displayName: "Colleague", mentionHandle: null, role: "member", listenMode: null, joinedAt: "2026-09-10T00:00:00Z", joinedSeq: 0 }] };
  mocks.rooms.mockReturnValue(state);
  render(<MessagingWorkspaceProvider><MessagingDirectory /><MessagingConversation /></MessagingWorkspaceProvider>);
  await waitFor(() => expect(panel.composer.sessionTargets?.map((t) => t.candidate.session.sessionId)).toEqual(["s1"]));
});

it("derives sharing from the workspace session list for room and directory participants", async () => {
  state.resourceSource.client.listWorkspaceSessions.mockResolvedValue({ sessions: [{ sessionId: "s1", humanUserId: "colleague", status: "busy" } as never] });
  state.activeRoom = { ...room, members: [...room.members, { kind: "human", id: "colleague", displayName: "Colleague", mentionHandle: null, role: "member", listenMode: null, joinedAt: "2026-09-10T00:00:00Z", joinedSeq: 0 }] };
  mocks.rooms.mockReturnValue(state);
  const view = render(<MessagingWorkspaceProvider><MessagingDirectory /><MessagingConversation /></MessagingWorkspaceProvider>);
  await waitFor(() => expect(panel.participants.find((p) => p.id === "colleague")?.sharing).toBe(true));
  expect(panel.participants.find((p) => p.id === "owner")?.sharing).toBe(false);
  expect(directory.participants.find((p) => p.id === "colleague")?.sharing).toBe(true);
  // Task 5 review finding: the own/ownerName mapping (sidebarSessions) was
  // untested because every context test mocked an empty session list.
  expect(directory.sessions?.find((row) => row.session.sessionId === "s1")).toMatchObject({ own: false, ownerName: "Colleague" });
  view.unmount();

  state.resourceSource.client.listWorkspaceSessions.mockResolvedValue({ sessions: [{ sessionId: "s2", humanUserId: "owner", status: "busy" } as never] });
  render(<MessagingWorkspaceProvider><MessagingDirectory /><MessagingConversation /></MessagingWorkspaceProvider>);
  await waitFor(() => expect(directory.sessions?.find((row) => row.session.sessionId === "s2")).toMatchObject({ own: true, ownerName: "You" }));
});

it("asks for ended sessions, keeps them out of sharing, baselines unread and marks the open session current", async () => {
  localStorage.clear();
  const at = (seq: number, status = "busy") => ({ ...(liveSession("s1", "colleague") as object), status, latestEventSeq: seq } as never);
  state.resourceSource.client.listWorkspaceSessions.mockResolvedValue({ sessions: [at(3), { ...(liveSession("old", "owner") as object), status: "ended", latestEventSeq: 1 } as never] });
  mocks.rooms.mockReturnValue(state);
  render(<MessagingWorkspaceProvider><MessagingDirectory /><MessagingConversation /></MessagingWorkspaceProvider>);
  await waitFor(() => expect(directory.sessions).toHaveLength(2));
  expect(state.resourceSource.client.listWorkspaceSessions).toHaveBeenCalledWith({ includeEnded: true });
  expect(directory.participants.find((p) => p.id === "owner")?.sharing).toBeFalsy();
  // First sight is the baseline: nothing unread yet.
  expect(directory.sessions?.find((row) => row.session.sessionId === "s1")?.unread).toBeNull();
  await act(async () => { await directory.onSelectSession?.("s1"); });
  await waitFor(() => expect(directory.currentSessionId).toBe("s1"));
  await waitFor(() => expect(directory.sessions?.find((row) => row.session.sessionId === "s1")?.unread ?? null).toBeNull());
});

it("offers channel admins only non-members and projects explicit member wire fields", async () => {
  state.activeRoom = { ...room, kind: "channel" };
  state.upsertMember.mockResolvedValue({ ...state.activeRoom, members: [...state.activeRoom.members] });
  compose();
  expect(panel.addMemberCandidates?.map((member) => member.id)).toEqual(["colleague", "agent-atlas"]);

  await act(async () => panel.onAddMember?.({ kind: "agent", id: "agent-atlas" }));

  expect(state.upsertMember).toHaveBeenCalledWith({
    kind: "agent", id: "agent-atlas", role: "member", listenMode: "active",
  });
  await act(async () => panel.onAddMember?.({ kind: "human", id: "colleague" }));
  expect(state.upsertMember).toHaveBeenLastCalledWith({
    kind: "human", id: "colleague", role: "member", listenMode: null,
  });
});

it("exposes only adding people for a direct message so the server can convert it in place", () => {
  state.activeRoom = { ...room, kind: "dm" };
  compose();
  expect(panel.onAddMember).toBeTypeOf("function");
  expect(panel.addMemberCandidates?.map((member) => member.id)).toEqual(["colleague", "agent-atlas"]);
  expect(panel.onRenameRoom).toBeUndefined();
  expect(panel.onRemoveMember).toBeUndefined();
});

it("lets a group chat member add people while preserving its existing editing rules", async () => {
  state.activeRoom = { ...room, kind: "group-dm" };
  compose();
  expect(panel.onAddMember).toBeTypeOf("function");
  expect(panel.onRemoveMember).toBeUndefined();
  await act(async () => panel.onRenameRoom?.("Project room"));
  expect(state.updateRoom).toHaveBeenCalledWith({ name: "Project room" });
});

it("gives a channel member without admin permission agent responses, and no other management", () => {
  state.activeRoom = { ...room, kind: "channel", members: room.members.map((member) => member.id === "owner" ? { ...member, role: "member" } : member) };
  compose();
  expect(panel.onAddMember).toBeUndefined();
  expect(panel.onRenameRoom).toBeUndefined();
  expect(panel.onRemoveMember).toBeUndefined();
  expect(panel.onListenModeChange).toBeDefined();
});

it.each(["channel", "dm", "group-dm"] as const)("lets room admins edit response modes in a %s", (kind) => {
  state.activeRoom = { ...room, kind, members: room.members.map((member) => member.id === "owner" ? { ...member, role: "admin" } : member) };
  compose();
  expect(panel.onListenModeChange).toBeTypeOf("function");
});

it.each([
  ["owner", "channel", true], ["admin", "channel", false], ["member", "channel", false],
  ["owner", "group-dm", false], ["owner", "dm", false],
] as const)("offers channel delete to a %s of a %s: %s", async (role, kind, offered) => {
  state.activeRoom = { ...room, kind, members: room.members.map((member) => member.id === "owner" ? { ...member, role } : member) };
  compose();
  expect(typeof panel.onDeleteRoom === "function").toBe(offered);
  await act(async () => panel.onDeleteRoom?.());
  if (offered) expect(state.deleteRoom).toHaveBeenCalledWith(room.roomId);
});

it.each(["owner", "admin"] as const)("connects channel %s management to existing commands", async (role) => {
  state.activeRoom = { ...room, kind: "channel", members: room.members.map((member) => member.id === "owner" ? { ...member, role } : member) };
  compose();
  await act(async () => panel.onRenameRoom?.("New name"));
  await act(async () => panel.onRemoveMember?.({ kind: "agent", id: "agent-friday" }));
  expect(state.updateRoom).toHaveBeenCalledWith({ name: "New name" });
  expect(state.removeMember).toHaveBeenCalledWith({ kind: "agent", id: "agent-friday" });
});

it("surfaces a failed room list through the existing retry control while retaining available rooms", () => {
  // There is no separate directory hydration any more — the room list IS the
  // directory, so its own error is the only one the sidebar can report.
  state.roomsError = new Error("Conversations could not be loaded");
  compose();
  expect(directory.state).toBe("error");
  expect(directory.rooms[0]).toMatchObject({ roomId: room.roomId, participants: [expect.objectContaining({ displayName: "Friday" })] });
  act(() => directory.onRetry?.());
  expect(state.refreshRooms).toHaveBeenCalledOnce();
});

it("keeps the directory usable when only the active room loses access", () => {
  state.accessRemoved = true;
  compose();
  expect(directory.state).toBe("ready");
  expect(panel.state).toBe("access-removed");
  act(() => directory.onSelectRoom("room-1"));
  expect(state.selectRoom).toHaveBeenCalledWith("room-1");
});

it("opens the selected hydrated room and opens creation without selecting a room", async () => {
  const onOpenRoom = vi.fn();
  compose(onOpenRoom);
  act(() => directory.onSelectRoom("room-1"));
  expect(state.selectRoom).toHaveBeenCalledWith("room-1");
  expect(onOpenRoom).toHaveBeenCalledWith(expect.objectContaining({ roomId: "room-1", participants: [expect.objectContaining({ id: "agent-friday" })] }));
  await act(async () => directory.onCreate?.({ kind: "channel", name: "Launch", members: [] }));
  expect(onOpenRoom).toHaveBeenLastCalledWith();
  expect(state.createRoom).toHaveBeenCalledWith(expect.objectContaining({ kind: "channel", name: "Launch" }));
});

it("opens Messages when a person or agent is selected from the directory", async () => {
  const onOpenRoom = vi.fn();
  compose(onOpenRoom);
  const participant = directory.participants[0];

  await act(async () => directory.onSelectParticipant(participant));

  expect(state.selectDm).toHaveBeenCalledWith(participant);
  expect(onOpenRoom).toHaveBeenCalledWith();
});

it("opens Messages when a session is selected from the sidebar, so its focus placement is visible", () => {
  const onOpenRoom = vi.fn();
  compose(onOpenRoom);
  act(() => directory.onSelectSession!("s1"));
  expect(onOpenRoom).toHaveBeenCalledWith();
});

it("surfaces DM selection failures through the directory action error", async () => {
  state.selectDm.mockRejectedValueOnce(new Error("Conversation could not be opened."));
  compose();

  await act(async () => directory.onSelectParticipant(directory.participants[0]));

  await waitFor(() => expect(directory.creationError).toBe("Conversation could not be opened."));
});

it("openMessage selects the room and exposes targetMessageId on roomProps, clearing it after 3s", () => {
  vi.useFakeTimers();
  state.historyComplete = true;
  compose();
  act(() => { workspaceValue.openMessage("room-1", "msg-1"); });
  expect(state.selectRoom).toHaveBeenCalledWith("room-1");
  expect(panel.targetMessageId).toBe("msg-1");
  act(() => { vi.advanceTimersByTime(3000); });
  expect(panel.targetMessageId).toBeNull();
  vi.useRealTimers();
});

it("loads older history for a message link before expiring its target", () => {
  vi.useFakeTimers();
  const view = compose();
  act(() => { workspaceValue.openMessage("room-1", "old-message"); });
  expect(state.loadHistory).toHaveBeenCalledOnce();
  act(() => { vi.advanceTimersByTime(3000); });
  expect(panel.targetMessageId).toBe("old-message");
  state.historyComplete = true;
  view.rerender(<MessagingWorkspaceProvider initialAgentId="agent-friday" subscribeToRoom>
    <CaptureContext /><MessagingDirectory /><MessagingConversation />
  </MessagingWorkspaceProvider>);
  act(() => { vi.advanceTimersByTime(3000); });
  expect(panel.targetMessageId).toBeNull();
  vi.useRealTimers();
});

it("renders the empty conversation when no room is selected", () => {
  state.activeRoomId = null;
  compose();
  expect(screen.getByRole("region", { name: "No chat selected" })).toBeInTheDocument();
});

it.each([
  ["dm", [room.members[0], room.members[1]], "agent-friday"],
  ["channel", room.members, undefined],
  ["group-dm", room.members, undefined],
  ["dm", [room.members[0]], undefined],
  ["dm", [...room.members, { ...room.members[1], id: "agent-atlas" }], undefined],
] as const)("scopes focus requests to a DM with one agent counterpart (%s)", (kind, members, expected) => {
  state.activeRoom = { ...room, kind, members: [...members] };
  compose();
  expect(panel.composer.focusAgentId).toBe(expected);
});

it("preserves a pending draft and focus when a conversation first mounts", () => {
  renderComposer = true;
  requestFocusComposer("agent-friday", "File the inbox");
  compose();
  const field = screen.getByRole("textbox", { name: "Message" });
  expect(field).toHaveValue("File the inbox");
  expect(field).toHaveFocus();
});

it("clears the old room's draft and attempt IDs on room change", () => {
  const view = compose();
  act(() => {
    panel.composer.onTextChange("Old draft");
    panel.onSelectThread?.("message-1");
  });
  act(() => {
    panel.threadComposer!.composer.onTextChange("Old reply");
  });
  const oldMessageId = panel.composer.clientMessageId;
  const oldThreadId = panel.threadComposer!.composer.clientMessageId;
  state.activeRoomId = "room-2";
  state.activeRoom = { ...room, roomId: "room-2" };
  view.rerender(<MessagingWorkspaceProvider initialAgentId="agent-friday" subscribeToRoom>
    <MessagingDirectory /><MessagingConversation />
  </MessagingWorkspaceProvider>);
  expect(panel.composer.text).toBe("");
  expect(panel.composer.clientMessageId).not.toBe(oldMessageId);
  expect(panel.threadComposer).toBeUndefined();
  act(() => panel.onSelectThread?.("message-2"));
  expect(panel.threadComposer!.composer.text).toBe("");
  expect(panel.threadComposer!.composer.clientMessageId).not.toBe(oldThreadId);
});

it("does not clear the new room's draft when the old room's send finishes", async () => {
  let resolveSend!: () => void;
  state.sendMessage.mockImplementationOnce(() => new Promise<void>((resolve) => { resolveSend = resolve; }));
  const view = compose();
  const request = {
    clientMessageId: panel.composer.clientMessageId,
    text: "Send from A",
    mentions: [],
    replyToMessageId: null,
    threadRootMessageId: null,
  };
  let sending!: Promise<void>;
  act(() => { sending = Promise.resolve(panel.composer.onSubmit(request)); });

  state.activeRoomId = "room-2";
  state.activeRoom = { ...room, roomId: "room-2" };
  view.rerender(<MessagingWorkspaceProvider initialAgentId="agent-friday" subscribeToRoom>
    <MessagingDirectory /><MessagingConversation />
  </MessagingWorkspaceProvider>);
  act(() => panel.composer.onTextChange("Keep this draft in B"));
  await act(async () => {
    resolveSend();
    await sending;
  });

  expect(panel.composer.text).toBe("Keep this draft in B");
});

it("shows a message as pending while it is in flight, even though the composer has moved on", async () => {
  // The composer empties and takes a fresh client id the instant a send leaves,
  // so the optimistic row can no longer be found by asking the composer what it
  // is holding. If it is, the bubble matches nothing and the message is
  // invisible until the server answers — worse than the frozen box it replaced.
  let resolveSend!: () => void;
  compose();
  const sentId = panel.composer.clientMessageId;
  const request = { clientMessageId: sentId, text: "In flight", mentions: [], replyToMessageId: null, threadRootMessageId: null };
  // What useRooms.sendMessage does synchronously before it awaits.
  state.sendMessage.mockImplementationOnce(() => {
    state.optimisticMessages = [{ ...request, roomId: room.roomId, createdAt: "2026-09-10T00:00:00Z" }];
    return new Promise<void>((resolve) => { resolveSend = resolve; });
  });
  act(() => panel.composer.onTextChange("In flight"));
  let sending!: Promise<void>;
  act(() => { sending = Promise.resolve(panel.composer.onSubmit(request)); });

  expect(panel.composer.text).toBe("");
  expect(panel.composer.clientMessageId).not.toBe(sentId);
  expect(panel.pendingMessages?.map((message) => message.clientMessageId)).toContain(sentId);

  await act(async () => { resolveSend(); await sending; });
});

it("hands a failed send back its text and its identity so retry still reaches it", async () => {
  // On failure `useRooms` drops the optimistic row and the draft has already
  // been cleared, so unless the composer re-adopts the failed id the message is
  // gone: nothing rendered, nothing to retry, and both "Retry message" and
  // "Check message status" would act on an id that tracks no request.
  state.sendMessage.mockImplementationOnce(() => Promise.reject(new Error("network")));
  compose();
  const sentId = panel.composer.clientMessageId;
  act(() => panel.composer.onTextChange("Please come back"));
  const request = { clientMessageId: sentId, text: "Please come back", mentions: [], replyToMessageId: null, threadRootMessageId: null };

  await act(async () => {
    await Promise.resolve(panel.composer.onSubmit(request)).catch(() => {});
  });

  expect(panel.composer.text).toBe("Please come back");
  expect(panel.composer.clientMessageId).toBe(sentId);
});

it("moves the composer on once Check message status confirms the send", async () => {
  // The composer re-adopted the uncertain send's id and text; a confirmed replay
  // must free it, or it stays locked with the sent text in it.
  state.sendMessage.mockImplementationOnce(() => Promise.reject(new Error("network")));
  state.reconcileSend.mockResolvedValueOnce({ messageId: "m1" });
  compose();
  const sentId = panel.composer.clientMessageId;
  act(() => panel.composer.onTextChange("Did it land?"));
  const request = { clientMessageId: sentId, text: "Did it land?", mentions: [], replyToMessageId: null, threadRootMessageId: null };
  await act(async () => { await Promise.resolve(panel.composer.onSubmit(request)).catch(() => {}); });
  expect(panel.composer.clientMessageId).toBe(sentId);

  await act(async () => { panel.composer.onReconcile!(sentId); });

  expect(state.reconcileSend).toHaveBeenCalledWith(sentId);
  await waitFor(() => expect(panel.composer.clientMessageId).not.toBe(sentId));
  expect(panel.composer.text).toBe("");
});

it("recovers a failed send into the room it was sent from when the owner has moved on, and shows it in the sidebar", async () => {
  // The other half of the recovery path: the composer is showing a different
  // room by the time the send fails, so the text cannot go back into it. It
  // goes back to the sending room's stored draft instead — and the sidebar has
  // to notice, which it will not do off `draft`/`activeRoomId` alone because
  // neither moves when a background room's draft is written.
  let rejectSend!: (error: Error) => void;
  state.sendMessage.mockImplementationOnce(() => new Promise<void>((_, reject) => { rejectSend = reject; }));
  const view = compose();
  act(() => panel.composer.onTextChange("Sent from room-1"));
  const request = {
    clientMessageId: panel.composer.clientMessageId,
    text: "Sent from room-1",
    mentions: [],
    replyToMessageId: null,
    threadRootMessageId: null,
  };
  let sending!: Promise<void>;
  act(() => { sending = Promise.resolve(panel.composer.onSubmit(request)).catch(() => {}); });

  state.activeRoomId = "room-2";
  state.activeRoom = { ...room, roomId: "room-2" };
  state.rooms = [room, { ...room, roomId: "room-2" }];
  view.rerender(<MessagingWorkspaceProvider initialAgentId="agent-friday" subscribeToRoom>
    <CaptureContext /><MessagingDirectory /><MessagingConversation />
  </MessagingWorkspaceProvider>);

  await act(async () => { rejectSend(new Error("network")); await sending; });

  // Stored against the room it was sent FROM, not the one now on screen.
  expect(JSON.parse(localStorage.getItem(draftKey("org", "room-1", null))!).text).toBe("Sent from room-1");
  expect(panel.composer.text).toBe("");
  // And the sidebar knows about it without waiting for an unrelated render.
  expect(directory.draftRoomIds).toContain("room-1");
});

it("does not overwrite text typed into the composer while an earlier send was still in flight", async () => {
  // The other door onto the same data loss: typing does not rotate the client
  // id, it only fills the box. So checking the id alone still let a failed send
  // overwrite text the owner had typed but not yet sent.
  let rejectSend!: (error: Error) => void;
  state.sendMessage.mockImplementationOnce(() => new Promise<void>((_, reject) => { rejectSend = reject; }));
  compose();
  const sentId = panel.composer.clientMessageId;
  act(() => panel.composer.onTextChange("the one that fails"));
  let sending!: Promise<void>;
  act(() => { sending = Promise.resolve(panel.composer.onSubmit({
    clientMessageId: sentId, text: "the one that fails", mentions: [], replyToMessageId: null, threadRootMessageId: null,
  })).catch(() => {}); });

  // Same composer instance, no second send — just typing.
  const liveId = panel.composer.clientMessageId;
  act(() => panel.composer.onTextChange("something else entirely"));

  await act(async () => { rejectSend(new Error("network")); await sending; });

  expect(panel.composer.text).toBe("something else entirely");
  expect(panel.composer.clientMessageId).toBe(liveId);
});

it("does not clobber newer unsent work when an earlier send fails", async () => {
  // Two sends in flight and the owner typing into the box the second one left
  // behind. Re-adopting the first message's id here would remount the composer
  // and take the typed text (and any attached-but-unsent image) with it. The
  // live text is the owner's current intent and exists nowhere else, so it
  // wins; the failed message's text is dropped rather than overwrite it.
  let rejectFirst!: (error: Error) => void;
  state.sendMessage
    .mockImplementationOnce(() => new Promise<void>((_, reject) => { rejectFirst = reject; }))
    .mockImplementationOnce(() => Promise.resolve());
  compose();

  const firstId = panel.composer.clientMessageId;
  act(() => panel.composer.onTextChange("first"));
  let firstSend!: Promise<void>;
  act(() => { firstSend = Promise.resolve(panel.composer.onSubmit({
    clientMessageId: firstId, text: "first", mentions: [], replyToMessageId: null, threadRootMessageId: null,
  })).catch(() => {}); });

  const secondId = panel.composer.clientMessageId;
  act(() => panel.composer.onTextChange("second"));
  await act(async () => { await Promise.resolve(panel.composer.onSubmit({
    clientMessageId: secondId, text: "second", mentions: [], replyToMessageId: null, threadRootMessageId: null,
  })); });

  // The owner is now typing a third message into a composer that has rotated
  // twice since the first send left.
  act(() => panel.composer.onTextChange("third draft, still being written"));
  const liveId = panel.composer.clientMessageId;

  await act(async () => { rejectFirst(new Error("network")); await firstSend; });

  expect(panel.composer.text).toBe("third draft, still being written");
  expect(panel.composer.clientMessageId).toBe(liveId);
  // And the live text was not overwritten in storage by the failed one either.
  expect(JSON.parse(localStorage.getItem(draftKey("org", "room-1", null))!).text)
    .toBe("third draft, still being written");
});

it("clears the sending room's stored draft when the owner switches rooms mid-send", async () => {
  let resolveSend!: () => void;
  state.sendMessage.mockImplementationOnce(() => new Promise<void>((resolve) => { resolveSend = resolve; }));
  const view = compose();
  act(() => panel.composer.onTextChange("Send from A"));
  expect(localStorage.getItem(draftKey("org", "room-1", null))).not.toBeNull();
  const request = {
    clientMessageId: panel.composer.clientMessageId,
    text: "Send from A",
    mentions: [],
    replyToMessageId: null,
    threadRootMessageId: null,
  };
  let sending!: Promise<void>;
  act(() => { sending = Promise.resolve(panel.composer.onSubmit(request)); });

  state.activeRoomId = "room-2";
  state.activeRoom = { ...room, roomId: "room-2" };
  view.rerender(<MessagingWorkspaceProvider initialAgentId="agent-friday" subscribeToRoom>
    <MessagingDirectory /><MessagingConversation />
  </MessagingWorkspaceProvider>);
  await act(async () => {
    resolveSend();
    await sending;
  });

  expect(localStorage.getItem(draftKey("org", "room-1", null))).toBeNull();
});

it("emptying the box drops the room from Drafts in the same render (dev walk R10)", () => {
  const view = compose();
  // Typing: the stored draft lands after each render, so the sidebar lists the room from the next keystroke.
  act(() => { panel.composer.onTextChange("make it"); });
  act(() => { panel.composer.onTextChange("make it one line"); });
  expect(directory.draftRoomIds).toContain("room-1");
  act(() => { panel.composer.onTextChange(""); });
  expect(directory.draftRoomIds).not.toContain("room-1");
  expect(localStorage.getItem(draftKey("org", "room-1", null))).toBeNull();
  view.unmount();
});

describe("the front agent before the roster loads (dev walk R6)", () => {
  afterEach(() => { teams.agentsLoading = false; teams.superAgent = false; });
  const atlasRoom = { ...room, members: [room.members[0], { ...room.members[1], id: "agent-atlas", displayName: "CEO", mentionHandle: "ceo" }] };

  it("never reads its seat name: Lightbulb from the last load, or by seat name on a first load", () => {
    teams.superAgent = true;
    state.activeRoom = atlasRoom; state.rooms = [atlasRoom];
    // A load that knows the roster remembers who the front agent is.
    const first = compose();
    expect(panel.participants.find((p) => p.id === "agent-atlas")?.displayName).toBe("Lightbulb");
    first.unmount();
    // A hard load: the roster hasn't answered yet, and the room still says "CEO".
    teams.agentsLoading = true;
    const second = compose();
    expect(panel.participants.find((p) => p.id === "agent-atlas")).toMatchObject({ displayName: "Lightbulb", mentionHandle: "lightbulb" });
    expect(directory.ceoAgentId).toBe("agent-atlas");
    second.unmount();
    // A first load ever (nothing remembered): the seat name still reads Lightbulb.
    localStorage.clear();
    const third = compose();
    expect(panel.participants.find((p) => p.id === "agent-atlas")?.displayName).toBe("Lightbulb");
    third.unmount();
  });
});

it("restores a per-room draft when the owner returns to the room", () => {
  const view = compose();
  act(() => { panel.composer.onTextChange("Draft in A"); });

  state.activeRoomId = "room-2";
  state.activeRoom = { ...room, roomId: "room-2" };
  view.rerender(<MessagingWorkspaceProvider initialAgentId="agent-friday" subscribeToRoom>
    <MessagingDirectory /><MessagingConversation />
  </MessagingWorkspaceProvider>);
  expect(panel.composer.text).toBe("");

  state.activeRoomId = room.roomId;
  state.activeRoom = room;
  view.rerender(<MessagingWorkspaceProvider initialAgentId="agent-friday" subscribeToRoom>
    <MessagingDirectory /><MessagingConversation />
  </MessagingWorkspaceProvider>);
  expect(panel.composer.text).toBe("Draft in A");
});

it("moves to the next room on Alt+ArrowDown and the next unread on Alt+Shift+ArrowDown", () => {
  const unreadRoom = { ...room, roomId: "room-2", unreadCount: 2 };
  const thirdRoom = { ...room, roomId: "room-3" };
  // The adjacent room is deliberately NOT the unread one, so the two
  // shortcuts cannot pass by landing on the same room.
  state.rooms = [room, thirdRoom, unreadRoom];
  compose();

  fireEvent.keyDown(window, { key: "ArrowDown", altKey: true });
  expect(state.selectRoom).toHaveBeenCalledWith(thirdRoom.roomId);

  fireEvent.keyDown(window, { key: "ArrowDown", altKey: true, shiftKey: true });
  expect(state.selectRoom).toHaveBeenLastCalledWith(unreadRoom.roomId);
});

it("follows the sidebar's order on Alt+ArrowDown, not the server's", () => {
  state.rooms = [room, { ...room, roomId: "room-2" }, { ...room, roomId: "room-3" }];
  const sidebar = document.createElement("nav");
  sidebar.className = "room-directory";
  for (const id of ["room-3", "room-1", "room-2"]) {
    const row = document.createElement("button");
    row.dataset.roomId = id;
    sidebar.append(row);
  }
  document.body.append(sidebar);
  compose();

  fireEvent.keyDown(window, { key: "ArrowDown", altKey: true });
  expect(state.selectRoom).toHaveBeenCalledWith("room-2");
  fireEvent.keyDown(window, { key: "ArrowUp", altKey: true });
  expect(state.selectRoom).toHaveBeenLastCalledWith("room-3");
  sidebar.remove();
});

it("moves rooms on Alt+ArrowUp from inside the composer instead of editing the last message", () => {
  renderComposer = true;
  state.rooms = [room, { ...room, roomId: "room-3" }];
  compose();

  fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "ArrowUp", altKey: true });

  expect(state.selectRoom).toHaveBeenCalledWith("room-3");
  expect(editLast).not.toHaveBeenCalled();
});

it("keeps a muted room's unread out of the browser-tab title", () => {
  state.rooms = [{ ...room, unreadCount: 3, muted: true }];
  const view = compose();
  expect(document.title).not.toMatch(/^\(\d+\)/);
  view.unmount();

  state.rooms = [{ ...room, unreadCount: 3, muted: false }];
  compose();
  expect(document.title).toMatch(/^\(3\)/);
});

it("counts only the viewer's own waiting sessions in the tab title, and none when waiting is off", async () => {
  const waiting = (sessionId: string, humanUserId: string) => ({ sessionId, humanUserId, status: "waiting-approval", provider: "claude", title: sessionId } as never);
  state.resourceSource.client.listWorkspaceSessions.mockResolvedValue({ sessions: [waiting("mine", "owner"), waiting("theirs", "colleague")] });
  mocks.waitingPref = "badge";
  const view = compose();
  await waitFor(() => expect(document.title).toMatch(/^\(1\)/));
  view.unmount();

  mocks.waitingPref = "off";
  compose();
  await act(async () => {});
  expect(document.title).not.toMatch(/^\(\d+\)/);
  mocks.waitingPref = "banner";
});

it("opens the shortcut sheet on Cmd+/", () => {
  compose();
  fireEvent.keyDown(window, { key: "/", metaKey: true });
  expect(screen.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeInTheDocument();
});

it("does not stack the shortcut sheet over another open dialog", () => {
  compose();
  const palette = document.body.appendChild(document.createElement("div"));
  palette.setAttribute("role", "dialog");

  fireEvent.keyDown(window, { key: "/", metaKey: true });
  palette.remove();

  expect(screen.queryByRole("dialog", { name: "Keyboard shortcuts" })).not.toBeInTheDocument();
});

it("closes the shortcut sheet on a second Cmd+/, even though it is itself an open dialog", () => {
  compose();
  fireEvent.keyDown(window, { key: "/", metaKey: true });
  expect(screen.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeInTheDocument();
  fireEvent.keyDown(window, { key: "/", metaKey: true });
  expect(screen.queryByRole("dialog", { name: "Keyboard shortcuts" })).not.toBeInTheDocument();
});

it("opens someone else's session from the sidebar in focus on its own, and clears it when a conversation is picked", async () => {
  state.resourceSource.client.listWorkspaceSessions.mockResolvedValue({ sessions: [{ sessionId: "s1", humanUserId: "colleague", status: "idle" } as never] });
  mocks.rooms.mockReturnValue(state);
  render(<MessagingWorkspaceProvider><CaptureContext /><MessagingDirectory /></MessagingWorkspaceProvider>);
  await waitFor(() => expect(directory.sessions).toHaveLength(1));
  act(() => directory.onSelectSession!("s1"));
  // Sessions are workspace-scoped: no DM is opened and no room is chosen for them.
  expect(state.selectDm).not.toHaveBeenCalled();
  expect(state.selectRoom).not.toHaveBeenCalled();
  expect(workspaceValue.sessionFocus).toEqual({ sessionId: "s1" });
  act(() => directory.onSelectRoom("room-1"));
  expect(workspaceValue.sessionFocus).toBeNull();
});

/** A session complete enough for the focused panel to render. */
const liveSession = (sessionId: string, humanUserId: string) => ({ sessionId, humanUserId, deviceId: "mac", provider: "claude", generation: "g", status: "busy",
  providerVersion: "test", activeTurnId: null, capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true,
  controlUnavailableReason: null, latestEventSeq: 0 }) as never;

/** Holds the provider's workspace-session fetch open (a cold load) until the test lands it. */
function holdSessionList() {
  let land!: (page: { sessions: never[] }) => void;
  state.resourceSource.client.listWorkspaceSessions.mockImplementation(() => new Promise((resolve) => { land = resolve; }));
  return (sessions: never[]) => act(async () => { land({ sessions }); });
}

it("holds a permalink until the workspace session list lands, then opens it in focus", async () => {
  const landList = holdSessionList();
  mocks.rooms.mockReturnValue(state);
  render(<MessagingWorkspaceProvider><CaptureContext /><MessagingConversation /></MessagingWorkspaceProvider>);
  act(() => workspaceValue.openSession("s1"));
  // Prod 2026-10-07: the restored room and its live composer showed here until the list landed,
  // so a person typing right after a reload posted into the wrong room.
  expect(workspaceValue.sessionFocus).toEqual({ sessionId: "s1" });
  expect(screen.getByRole("region", { name: "Shared session" })).toBeInTheDocument();
  expect(screen.getByText("Opening session…")).toBeInTheDocument();
  expect(document.querySelector(".room-composer, textarea")).toBeNull();
  expect(mocks.rooms.mock.calls.at(-1)?.[2]).toBe(false); // the covered room does not auto-read either
  await landList([liveSession("s1", "colleague")]);
  expect(workspaceValue.sessionFocus).toEqual({ sessionId: "s1" });
  expect(screen.getByRole("region", { name: "Shared session" })).toBeInTheDocument();
  expect(screen.queryByText("Opening session…")).not.toBeInTheDocument();
});

it("counts a person's session open as a pick, but not a held link's later replay", async () => {
  const landList = holdSessionList();
  mocks.rooms.mockReturnValue(state);
  render(<MessagingWorkspaceProvider><CaptureContext /><MessagingConversation /></MessagingWorkspaceProvider>);
  const before = workspaceValue.explicitPickCount;
  act(() => workspaceValue.openSession("s1"));
  await landList([liveSession("s1", "colleague")]);
  expect(workspaceValue.sessionFocus).toEqual({ sessionId: "s1" });
  expect(workspaceValue.explicitPickCount).toBe(before);
  act(() => workspaceValue.openSession("s2"));
  expect(workspaceValue.sessionFocus).toEqual({ sessionId: "s2" });
  expect(workspaceValue.explicitPickCount).toBe(before + 1);
});

it("keeps a search hit's turn when the URL it wrote replays the same session", async () => {
  const landList = holdSessionList();
  mocks.rooms.mockReturnValue(state);
  render(<MessagingWorkspaceProvider><CaptureContext /><MessagingConversation /></MessagingWorkspaceProvider>);
  await landList([liveSession("s1", "colleague"), liveSession("s2", "colleague")]);
  act(() => workspaceValue.openSession("s1", false, 4));
  expect(workspaceValue.sessionFocus).toEqual({ sessionId: "s1", seq: 4 });
  // Dev 2026-10-27: the shell replayed ?session=s1 (no seq) and the conversation opened at the bottom.
  act(() => workspaceValue.openSession("s1"));
  expect(workspaceValue.sessionFocus).toEqual({ sessionId: "s1", seq: 4 });
  act(() => workspaceValue.openSession("s2"));
  expect(workspaceValue.sessionFocus).toEqual({ sessionId: "s2" });
});

it("says a linked session that the settled list lacks is no longer shared", async () => {
  const landList = holdSessionList();
  mocks.rooms.mockReturnValue(state);
  render(<MessagingWorkspaceProvider><CaptureContext /><MessagingConversation /></MessagingWorkspaceProvider>);
  act(() => workspaceValue.openSession("old"));
  await landList([]);
  expect(workspaceValue.sessionFocus).toEqual({ sessionId: "old" });
  // The settled list lacks it, so it is fetched directly; a 404 is "no longer shared".
  expect(await screen.findByText("This session is no longer shared.")).toBeInTheDocument();
});

it("drops a held permalink when a conversation is picked before the session list lands", async () => {
  const landList = holdSessionList();
  mocks.rooms.mockReturnValue(state);
  render(<MessagingWorkspaceProvider><CaptureContext /><MessagingDirectory /><MessagingConversation /></MessagingWorkspaceProvider>);
  act(() => workspaceValue.openSession("s1"));
  act(() => directory.onSelectRoom("room-1"));
  await landList([{ sessionId: "s1", humanUserId: "colleague", status: "idle" } as never]);
  expect(workspaceValue.sessionFocus).toBeNull();
});

it("clears focus when the active room moves off by any path", async () => {
  state.resourceSource.client.listWorkspaceSessions.mockResolvedValue({ sessions: [{ sessionId: "s1", humanUserId: "colleague", status: "idle" } as never] });
  state.rooms = [room, { ...room, roomId: "room-2" }];
  mocks.rooms.mockImplementation(function useRoomsWithRealSelection() {
    const [activeRoomId, setActiveRoomId] = useState<string | null>(room.roomId);
    return { ...state, activeRoomId, activeRoom: activeRoomId ? { ...room, roomId: activeRoomId } : null,
      selectRoom: (roomId: string) => { state.selectRoom(roomId); setActiveRoomId(roomId); } };
  });
  render(<MessagingWorkspaceProvider><CaptureContext /><MessagingDirectory /></MessagingWorkspaceProvider>);
  await waitFor(() => expect(directory.sessions).toHaveLength(1));
  act(() => workspaceValue.openSession("s1"));
  expect(workspaceValue.sessionFocus).toEqual({ sessionId: "s1" });
  // ⌥↓ (like a notification click) moves the active room without the directory.
  fireEvent.keyDown(window, { key: "ArrowDown", altKey: true });
  expect(workspaceValue.activeRoomId).toBe("room-2");
  expect(workspaceValue.sessionFocus).toBeNull();
});

it("treats the room as hidden while a session covers it", async () => {
  state.resourceSource.client.listWorkspaceSessions.mockResolvedValue({ sessions: [{ sessionId: "s1", humanUserId: "colleague", status: "busy" } as never] });
  mocks.rooms.mockReturnValue(state);
  render(<MessagingWorkspaceProvider><CaptureContext /><MessagingDirectory /></MessagingWorkspaceProvider>);
  await waitFor(() => expect(directory.sessions).toHaveLength(1));
  expect(notifications.roomVisible).toBe(true);
  act(() => directory.onSelectSession!("s1"));
  // Neither auto-read nor notification suppression: nothing of the room is on screen.
  expect(notifications.roomVisible).toBe(false);
  expect(mocks.rooms).toHaveBeenLastCalledWith(undefined, true, false, true, true);
  act(() => workspaceValue.closeSession());
  expect(notifications.roomVisible).toBe(true);
  expect(mocks.rooms).toHaveBeenLastCalledWith(undefined, true, true, true, true);
  // A notification for the covered room, clicked: "View" has to show the room.
  act(() => directory.onSelectSession!("s1"));
  act(() => notifications.onOpen("room-1", null));
  expect(workspaceValue.sessionFocus).toBeNull();
  expect(notifications.roomVisible).toBe(true);
});

it("keeps the focused session mounted while Messages is not the active dock panel", async () => {
  state.resourceSource.client.listWorkspaceSessions.mockResolvedValue({ sessions: [liveSession("mine", "owner")] });
  mocks.rooms.mockReturnValue(state);
  const tree = (subscribeToRoom: boolean) => <MessagingWorkspaceProvider subscribeToRoom={subscribeToRoom}>
    <CaptureContext /><MessagingDirectory /><MessagingConversation />
  </MessagingWorkspaceProvider>;
  const view = render(tree(true));
  await waitFor(() => expect(directory.sessions).toHaveLength(1));
  act(() => directory.onSelectSession!("mine"));
  const focused = screen.getByRole("region", { name: "Shared session" });
  // A split dock: Messages is on screen but another panel is active.
  view.rerender(tree(false));
  expect(screen.getByRole("region", { name: "Shared session" })).toBe(focused);
});

it("loads shared sessions on a cold start with Tasks focused", async () => {
  state.resourceSource.client.listWorkspaceSessions.mockResolvedValue({ sessions: [liveSession("mine", "owner")] });
  render(<MessagingWorkspaceProvider subscribeToRoom={false}><MessagingDirectory /></MessagingWorkspaceProvider>);
  await waitFor(() => expect(directory.sessions).toHaveLength(1));
});

it("lets a feed Discuss reply @mention any roster agent, though agents are not seated in the feed", () => {
  const feed: RoomDetail = { ...room, roomId: "feed-1", kind: "channel", name: "feed", feed: true, members: [room.members[0]] };
  mocks.rooms.mockReturnValue({ ...state, rooms: [], activeRoomId: feed.roomId, activeRoom: feed });
  compose();
  expect(panel.participants.map((member) => [member.id, member.mentionHandle])).toEqual([["owner", null], ["agent-friday", null], ["agent-atlas", null]]);
  expect(resolveTypedMentions("@Atlas can you check this?", panel.participants)).toEqual([{ kind: "agent", id: "agent-atlas" }]);
});

it("offers the feed none of a channel's managed actions, only its topic (the brief)", () => {
  const feed: RoomDetail = { ...room, roomId: "feed-1", kind: "channel", name: "feed", feed: true, members: [room.members[0]] };
  mocks.rooms.mockReturnValue({ ...state, rooms: [], activeRoomId: feed.roomId, activeRoom: feed });
  compose();
  for (const action of ["onRenameRoom", "onLeaveRoom", "onSetArchived", "onDeleteRoom", "onAddMember", "onRemoveMember", "onCopyJoinLink", "onListenModeChange"] as const) {
    expect(panel[action], action).toBeUndefined();
  }
  expect(panel.onSetTopic).toEqual(expect.any(Function));
});

it("keeps history action inputs stable while the provider draft or live run changes", () => {
  // The real provider makes these callbacks: a RoomPanel-only fixture with
  // stable functions would miss provider churn that defeats history's memo.
  renderComposer = true;
  // Like the shared hook: a fresh result object, stable unchanged fields.
  mocks.rooms.mockImplementation(() => ({ ...state }));
  const view = compose();
  const before = {
    select: panel.onSelectParticipant,
    unread: panel.onMarkUnreadFrom,
    pins: panel.pinnedMessageIds,
    participants: panel.participants,
  };
  fireEvent.change(screen.getByRole("textbox", { name: "Message" }), { target: { value: "typing a draft" } });
  expect(panel.composer.text).toBe("typing a draft");
  expect(panel.onSelectParticipant).toBe(before.select);
  expect(panel.onMarkUnreadFrom).toBe(before.unread);
  expect(panel.pinnedMessageIds).toBe(before.pins);
  expect(panel.participants).toBe(before.participants);
  state.runs = [{ roomId: room.roomId, agentId: "agent-friday", runId: "stream", text: "Streaming text", thinking: "", state: "running", frames: [] }];
  view.rerender(<MessagingWorkspaceProvider initialAgentId="agent-friday" subscribeToRoom>
    <CaptureContext /><MessagingDirectory /><MessagingConversation />
  </MessagingWorkspaceProvider>);
  expect(panel.runs?.[0].text).toBe("Streaming text");
  expect(panel.onSelectParticipant).toBe(before.select);
  expect(panel.onMarkUnreadFrom).toBe(before.unread);
  expect(panel.pinnedMessageIds).toBe(before.pins);
  expect(panel.participants).toBe(before.participants);
});

describe("opening #general once it is seeded", () => {
  const general = { ...room, roomId: "general", kind: "channel" as const, name: "general", isDefault: true };
  const vendor = { ...room, roomId: "vendor", kind: "channel" as const, name: "lightbulb", shared: true };
  const dm = room;
  // Stands in for the shared useRooms' own selection: it auto-picks rooms[0]
  // (most recently active) in an effect whenever nothing is active, and
  // selectRoom really moves the selection — so the ordering between that
  // pick and the provider's #general effect is exercised, not mocked away.
  let list: RoomDetail[] = [];
  const selectRoomSpy = vi.fn();
  function useSimulatedRooms() {
    const [activeRoomId, setActiveRoomId] = useState<string | null>(null);
    useEffect(() => {
      // Mirrors packages/shared useRooms' own auto-pick effect verbatim.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      if (!activeRoomId && list.length > 0) setActiveRoomId((current) => current ?? list[0].roomId);
    }, [activeRoomId]);
    const selectRoom = useCallback((roomId: string) => { selectRoomSpy(roomId); setActiveRoomId(roomId); }, []);
    const activeRoom = list.find((candidate) => candidate.roomId === activeRoomId) ?? null;
    return { ...state, rooms: list, activeRoomId, activeRoom, selectRoom, selectAgentDm: async () => null, directoryRoomDetails: new Map(list.map((r) => [r.roomId, r])) };
  }
  const shell = () => <MessagingWorkspaceProvider subscribeToRoom><CaptureContext /><MessagingDirectory /><MessagingConversation /></MessagingWorkspaceProvider>;
  beforeEach(() => {
    mocks.orgId = "org";
    selectRoomSpy.mockClear();
    mocks.rooms.mockImplementation(useSimulatedRooms);
  });
  afterEach(() => { mocks.orgId = "org"; });

  it("says the team is getting ready while there are no rooms, then opens #general exactly once", () => {
    list = [];
    const view = render(shell());
    expect(screen.getByRole("status")).toHaveTextContent("Your team is getting ready — #general will open here.");
    list = [general];
    view.rerender(shell());
    expect(workspaceValue.activeRoomId).toBe("general");
    view.rerender(shell());
    expect(selectRoomSpy.mock.calls.filter(([id]) => id === "general").length).toBeLessThanOrEqual(1);
  });

  it("opens #general in a fresh workspace even when the auto-pick landed on the shared room", () => {
    list = [vendor, general];
    render(shell());
    expect(workspaceValue.activeRoomId).toBe("general");
  });

  it("opens the first-run Lightbulb DM, not #general, in a fresh super-agent workspace", () => {
    const firstRun = { ...dm, roomId: "lightbulb-dm", firstRun: true };
    list = [general, vendor, firstRun];
    render(shell());
    expect(workspaceValue.activeRoomId).toBe("lightbulb-dm");
    expect(selectRoomSpy).not.toHaveBeenCalledWith("general");
  });

  it("leaves a returning viewer on their most recent room when any other room exists", () => {
    list = [dm, general, vendor];
    render(shell());
    expect(workspaceValue.activeRoomId).toBe(dm.roomId);
    expect(selectRoomSpy).not.toHaveBeenCalledWith("general");
  });

  it("an agent DM a person asked for counts as their choice; the default-agent fallback does not", async () => {
    list = [vendor];
    const view = render(shell());
    await act(async () => { await workspaceValue.selectAgentDm("agent-x"); });
    list = [vendor, general];
    view.rerender(shell());
    expect(selectRoomSpy).not.toHaveBeenCalledWith("general");

    mocks.orgId = "org-2";
    list = [vendor];
    view.rerender(shell());
    await act(async () => { await workspaceValue.selectAgentDm("default-agent", false); });
    list = [vendor, general];
    view.rerender(shell());
    expect(workspaceValue.activeRoomId).toBe("general");
  });

  it("never overrides a room the viewer chose", () => {
    list = [vendor];
    const view = render(shell());
    act(() => directory.onSelectRoom("vendor"));
    list = [vendor, general];
    view.rerender(shell());
    expect(workspaceValue.activeRoomId).toBe("vendor");
    expect(selectRoomSpy).not.toHaveBeenCalledWith("general");
  });

  it("does not re-open #general after the viewer navigates away", () => {
    list = [general, vendor];
    const view = render(shell());
    expect(workspaceValue.activeRoomId).toBe("general");
    act(() => directory.onSelectRoom("vendor"));
    view.rerender(shell());
    expect(workspaceValue.activeRoomId).toBe("vendor");
  });

  describe("the default agent (mobile web's fallback)", () => {
    const mobile = () => <MessagingWorkspaceProvider defaultAgentId="agent-ceo" subscribeToRoom><CaptureContext /><MessagingConversation /></MessagingWorkspaceProvider>;
    const lastInitialAgent = () => mocks.rooms.mock.calls.at(-1)?.[0];

    it("is not opened in a fresh workspace, which lands in #general", () => {
      list = [vendor, general];
      render(mobile());
      expect(workspaceValue.freshLanding).toBe(true);
      expect(lastInitialAgent()).toBeUndefined();
      expect(workspaceValue.activeRoomId).toBe("general");
    });

    it("is opened for a returning workspace, as before", () => {
      list = [dm, general, vendor];
      render(mobile());
      expect(workspaceValue.freshLanding).toBe(false);
      expect(lastInitialAgent()).toBe("agent-ceo");
    });

    it("waits for the room list, and a later DM does not reopen it", () => {
      mocks.rooms.mockImplementation(() => ({ ...useSimulatedRooms(), roomsLoading: list.length === 0 }));
      list = [];
      const view = render(mobile());
      expect(workspaceValue.freshLanding).toBeNull();
      expect(mocks.rooms.mock.calls.every(([agent]) => agent === undefined)).toBe(true);
      list = [general];
      view.rerender(mobile());
      list = [general, dm];
      view.rerender(mobile());
      expect(workspaceValue.freshLanding).toBe(true);
      expect(mocks.rooms.mock.calls.every(([agent]) => agent === undefined)).toBe(true);
    });

    describe("decides only when certain", () => {
      beforeEach(() => { vi.useFakeTimers(); });
      afterEach(() => { vi.useRealTimers(); });

      it("[] then [general] is fresh", () => {
        list = [];
        const view = render(mobile());
        expect(workspaceValue.freshLanding).toBeNull();
        list = [general];
        view.rerender(mobile());
        expect(workspaceValue.freshLanding).toBe(true);
      });

      it("[vendor] then [vendor, general] is fresh", () => {
        list = [vendor];
        const view = render(mobile());
        expect(workspaceValue.freshLanding).toBeNull();
        list = [vendor, general];
        view.rerender(mobile());
        expect(workspaceValue.freshLanding).toBe(true);
        expect(lastInitialAgent()).toBeUndefined();
      });

      it("[vendor] with no #general for a minute falls back to the default DM", () => {
        list = [vendor];
        render(mobile());
        act(() => { vi.advanceTimersByTime(59_999); });
        expect(workspaceValue.freshLanding).toBeNull();
        expect(lastInitialAgent()).toBeUndefined();
        act(() => { vi.advanceTimersByTime(1); });
        expect(workspaceValue.freshLanding).toBe(false);
        expect(lastInitialAgent()).toBe("agent-ceo");
      });

      it("[dm] is returning immediately", () => {
        list = [dm];
        render(mobile());
        expect(workspaceValue.freshLanding).toBe(false);
        expect(lastInitialAgent()).toBe("agent-ceo");
      });

      it("a transient [] then [dm, general] is returning", () => {
        list = [];
        const view = render(mobile());
        list = [dm, general];
        view.rerender(mobile());
        expect(workspaceValue.freshLanding).toBe(false);
        act(() => { vi.advanceTimersByTime(60_000); });
        expect(workspaceValue.freshLanding).toBe(false);
      });
    });

    it("never displaces an explicit agent", () => {
      list = [vendor, general];
      render(<MessagingWorkspaceProvider initialAgentId="agent-friday" defaultAgentId="agent-ceo" subscribeToRoom><CaptureContext /></MessagingWorkspaceProvider>);
      expect(lastInitialAgent()).toBe("agent-friday");
    });
  });

  it("re-arms for a second workspace, even after a choice in the first", () => {
    list = [general];
    const view = render(shell());
    expect(workspaceValue.activeRoomId).toBe("general");
    act(() => directory.onSelectRoom("general"));
    mocks.orgId = "org-2";
    list = [vendor, { ...general, roomId: "general-2" }];
    view.rerender(shell());
    expect(workspaceValue.activeRoomId).toBe("general-2");
  });
});

it("opens a session with its chat when a notification names the session's hidden room", async () => {
  state.resourceSource.client.listWorkspaceSessions.mockResolvedValue({ sessions: [] });
  state.resourceSource.client.getRoom.mockResolvedValueOnce({ ...room, roomId: "chat-1", kind: "channel", session: { sessionId: "s1" } });
  compose();
  await act(async () => {});
  act(() => notifications.onOpen("chat-1", null));
  await waitFor(() => expect(workspaceValue.sessionFocus).toEqual({ sessionId: "s1", chat: true }));
  expect(state.selectRoom).not.toHaveBeenCalledWith("chat-1");
});

it("lends the one live room subscription to the session chat while it is open", async () => {
  state.resourceSource.client.listWorkspaceSessions.mockResolvedValue({ sessions: [] });
  compose();
  await act(async () => {});
  act(() => workspaceValue.openSession("s1", true));
  expect(mocks.rooms.mock.calls.at(-1)?.[1]).toBe(false);
  act(() => workspaceValue.setSessionChat(false));
  expect(mocks.rooms.mock.calls.at(-1)?.[1]).toBe(true);
});

it("keeps the chat flag on a session link held for the session list (a cold-load notification tap)", async () => {
  const landList = holdSessionList();
  render(<MessagingWorkspaceProvider><CaptureContext /></MessagingWorkspaceProvider>);
  act(() => workspaceValue.openSession("s1", true));
  expect(workspaceValue.sessionFocus).toEqual({ sessionId: "s1", chat: true });
  await landList([liveSession("s1", "colleague")]);
  expect(workspaceValue.sessionFocus).toEqual({ sessionId: "s1", chat: true });
});

it("tells notifications the session chat on screen shows replies inline, and stops when it closes", async () => {
  state.resourceSource.client.listWorkspaceSessions.mockResolvedValue({ sessions: [] });
  compose();
  await act(async () => {});
  const side = () => (notifications as unknown as { sideRoomIds?: string[] }).sideRoomIds ?? [];
  expect(side()).toEqual([]);
  const main = (notifications as unknown as { activeRoomId: string | null }).activeRoomId;
  act(() => workspaceValue.openSession("s1", true));
  act(() => workspaceValue.setSessionChatRoomId("chat-1"));
  // An app's conversation in Canvas too: each side room is kept quiet on its own, and the main room stays as it was.
  act(() => workspaceValue.setSideRoom("app-conversation:a1", "conv-1"));
  expect(side()).toEqual(["chat-1", "conv-1"]);
  expect((notifications as unknown as { activeRoomId: string | null }).activeRoomId).toBe(main);
  act(() => workspaceValue.setSessionChatRoomId(null));
  expect(side()).toEqual(["conv-1"]);
  act(() => workspaceValue.setSideRoom("app-conversation:a1", null));
  expect(side()).toEqual([]);
});

describe("askLightbulb (Home's Ask box)", () => {
  it("with no DM yet: one resolve (which creates it), one send to that room, then that room opens; no second resolve", async () => {
    const sendMessage = vi.fn(async () => ({}));
    Object.assign(state.resourceSource.client, { sendMessage });
    state.selectDm.mockResolvedValue("dm-new");
    compose();
    await act(async () => { await workspaceValue.askLightbulb("Plan the launch"); });
    // The front agent is the roster's CEO seat (agent-atlas), never a specialist.
    expect(state.selectDm).toHaveBeenCalledTimes(1);
    expect(state.selectDm).toHaveBeenCalledWith({ kind: "agent", id: "agent-atlas" }, { activate: false });
    expect(state.createRoom).not.toHaveBeenCalled();
    expect(state.selectAgentDm).not.toHaveBeenCalled();
    expect(sendMessage).toHaveBeenCalledTimes(1);
    expect(sendMessage).toHaveBeenCalledWith("dm-new", expect.objectContaining({ text: "Plan the launch", mentions: [], threadRootMessageId: null }));
    expect(state.selectRoom).toHaveBeenCalledWith("dm-new");
    expect(sendMessage.mock.invocationCallOrder[0]).toBeLessThan(state.selectRoom.mock.invocationCallOrder[0]);
  });

  it("a failed send rejects and opens nothing", async () => {
    Object.assign(state.resourceSource.client, { sendMessage: vi.fn(async () => { throw new Error("offline"); }) });
    state.selectDm.mockResolvedValue("dm-new");
    compose();
    await act(async () => { await expect(workspaceValue.askLightbulb("Hi")).rejects.toThrow("offline"); });
    expect(state.selectRoom).not.toHaveBeenCalled();
  });
});

it("a drafted handle shows the draft mark at once, and clearing it removes the mark (dev walk W8-11)", () => {
  const view = compose();
  act(() => { panel.composer.onTextChange("@growth "); });
  expect(directory.draftRoomIds).toContain("room-1");
  act(() => { panel.composer.onTextChange(""); });
  expect(directory.draftRoomIds).not.toContain("room-1");
  expect(localStorage.getItem(draftKey("org", "room-1", null))).toBeNull();
  view.unmount();
});

it("W17-7: someone posting never writes back a draft that a send or another tab cleared", () => {
  const view = compose();
  act(() => { panel.composer.onTextChange("@growth "); });
  expect(localStorage.getItem(draftKey("org", "room-1", null))).not.toBeNull();
  localStorage.removeItem(draftKey("org", "room-1", null)); // sent from another tab
  // Growth posts in the DM: the room's people change, and this tab still holds the text it typed.
  state.activeRoom = { ...room, members: [...room.members, { ...room.members[1], id: "agent-growth", displayName: "Growth", mentionHandle: "growth" }] };
  view.rerender(<MessagingWorkspaceProvider initialAgentId="agent-friday" subscribeToRoom>
    <MessagingDirectory /><MessagingConversation />
  </MessagingWorkspaceProvider>);
  expect(localStorage.getItem(draftKey("org", "room-1", null))).toBeNull();
  view.unmount();
});

// Dev walk W9-2: Chrome runs queued key events before React's scheduled (default-lane) work, so every keystroke
// that left a default-lane update behind counted as one more nested update; past 50 the next keystroke's setState
// threw React #185 and that character was lost.
it("keeps every character when keys land faster than React's scheduled work (W9-2)", async () => {
  renderComposer = true;
  compose();
  const field = screen.getByRole("textbox", { name: "Message" }) as HTMLTextAreaElement;
  const typed = "Hi Lightbulb, email walk9-outsider+clerk_test@thelightbulb.company a one-line hello";
  const setValue = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
  const errors: unknown[] = [];
  const onError = (event: ErrorEvent) => { errors.push(event.error); event.preventDefault(); };
  window.addEventListener("error", onError);
  const actEnv = (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT;
  // Outside act(), like a browser: each input is a discrete (sync) render; anything else waits for the scheduler.
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = false;
  const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
  try {
    for (const char of typed) {
      setValue.call(field, field.value + char);
      field.dispatchEvent(new Event("input", { bubbles: true }));
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  } finally {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = actEnv;
    window.removeEventListener("error", onError);
  }
  const depth = [...errors, ...consoleError.mock.calls.flat()].filter((e) => String((e as Error)?.message ?? e).includes("Maximum update depth"));
  consoleError.mockRestore();
  expect(depth).toEqual([]);
  expect(field.value).toBe(typed);
  expect(panel.composer.text).toBe(typed);
});
