import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useEffect } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { RoomDetail } from "@lightbulb/shared";
import type { RoomDirectoryProps } from "../RoomDirectory";
import type { RoomPanelProps } from "../RoomPanel";
import { RoomComposer } from "../RoomComposer";
import { draftKey } from "../drafts";
import { requestFocusComposer, _resetFocusComposerRequestForTests } from "@/lib/focusComposerRequest";
import type { LiveRoomRun } from "@lightbulb/shared";

const mocks = vi.hoisted(() => ({ rooms: vi.fn(), agents: vi.fn(), roster: vi.fn() }));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ userId: "owner", orgId: "org" }) }));
vi.mock("@/hooks/useRooms", () => ({ useRooms: mocks.rooms }));
vi.mock("@/components/notifications/MessageNotifications", () => ({ MessageNotifications: () => null }));
vi.mock("@/hooks/useAgents", () => ({ useAgents: mocks.agents }));
vi.mock("@/components/agents/useRosterEntries", () => ({ useRosterEntries: mocks.roster }));
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({
  read: (path: string) => ({ data: path === "/agents"
    ? { agents: [{ id: "agent-friday", name: "Friday" }, { id: "agent-atlas", name: "Atlas" }] }
    : { members: [{ userId: "owner", name: "Owner" }, { userId: "colleague", name: "Colleague" }] } }),
}) }));
vi.mock("@/components/workspace/workspaceController", () => ({ openPanel: vi.fn() }));
let directory: RoomDirectoryProps;
let panel: RoomPanelProps;
let renderComposer = false;
vi.mock("../RoomDirectory", () => ({ RoomDirectory: (props: RoomDirectoryProps) => {
  directory = props;
  return <nav aria-label="Conversations" />;
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
  resourceSource: { client: { listResources: vi.fn(), listLocalSessionEvents: vi.fn(), submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn(), listWorkspaceSessions: vi.fn(async () => ({ sessions: [] as never[] })) }, onEvent: () => () => {}, connectionRevision: 1 },
  rooms: [room], roomsError: undefined as Error | undefined, roomsLoading: false,
  directoryError: undefined as Error | undefined, directoryLoading: false,
  activeRoomId: room.roomId as string | null, activeRoom: room,
  roomError: undefined, roomLoading: false, accessRemoved: false,
  messages: [], optimisticMessages: [] as Array<{ clientMessageId: string; roomId: string; text: string; mentions: never[]; replyToMessageId: null; threadRootMessageId: string | null; createdAt: string }>, typingMembers: [], runs: [] as LiveRoomRun[],
  historyLoading: false, historyComplete: false, historyError: false, historyErrorMessage: null as string | null,
  loadHistory: vi.fn(), selectRoomError: null as string | null,
  liveConnected: true, httpReady: true, selectRoom: vi.fn(), selectDm: vi.fn(), selectAgentDm: vi.fn(),
  directoryRoomDetails: new Map([[room.roomId, room]]),
  refreshRooms: vi.fn(), refreshRoom: vi.fn(), createRoom: vi.fn(), updateRoom: vi.fn(),
  upsertMember: vi.fn(), removeMember: vi.fn(), sendMessage: vi.fn(), reconcileSend: vi.fn(),
  sendState: () => "idle", setTyping: vi.fn(), abortRun: vi.fn(), steerRun: vi.fn(),
};
beforeEach(() => {
  vi.clearAllMocks();
  state.rooms = [room];
  state.activeRoomId = room.roomId;
  state.activeRoom = room;
  state.roomsError = undefined;
  state.optimisticMessages = [];
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
  expect(mocks.rooms).toHaveBeenCalledTimes(1);
  expect(mocks.rooms).toHaveBeenCalledWith("agent-friday", true);
  expect(state.selectAgentDm).not.toHaveBeenCalled();
  expect(directory.currentRoomId).toBe(panel.roomId);
  expect(screen.getByRole("navigation", { name: "Conversations" })).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Messages" })).toBeInTheDocument();
  view.unmount();
});

it("hands a session intent to the current room and cancels it on message navigation", () => {
  compose();
  act(() => workspaceValue.openSession("room-1", "session-1"));
  expect(panel.targetSession?.sessionId).toBe("session-1");
  const nonce = panel.targetSession?.nonce;
  act(() => workspaceValue.openSession("room-1", "session-1"));
  expect(panel.targetSession?.nonce).not.toBe(nonce);
  expect(panel.targetMessageId).toBeNull();
  act(() => workspaceValue.openMessage("room-1", "message-1"));
  expect(panel.targetSession).toBeNull();
  expect(panel.targetMessageId).toBe("message-1");
  expect(state.sendMessage).not.toHaveBeenCalled();
});

it("disables the resource pane transport while its workspace is hidden", () => {
  const view = render(<MessagingWorkspaceProvider subscribeToRoom><MessagingConversation /></MessagingWorkspaceProvider>);
  expect(panel.resourceSource).toBe(state.resourceSource);
  view.rerender(<MessagingWorkspaceProvider subscribeToRoom={false}><MessagingConversation /></MessagingWorkspaceProvider>);
  expect(panel.resourceSource).toBeUndefined();
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

it("offers channel admins only non-members and projects explicit member wire fields", async () => {
  state.activeRoom = { ...room, kind: "channel" };
  state.upsertMember.mockResolvedValue({ ...state.activeRoom, members: [...state.activeRoom.members] });
  compose();
  expect(panel.addMemberCandidates?.map((member) => member.id)).toEqual(["colleague", "agent-atlas"]);

  await act(async () => panel.onAddMember?.({ kind: "agent", id: "agent-atlas" }));

  expect(state.upsertMember).toHaveBeenCalledWith({
    kind: "agent", id: "agent-atlas", role: "member", listenMode: "mentions",
  });
  await act(async () => panel.onAddMember?.({ kind: "human", id: "colleague" }));
  expect(state.upsertMember).toHaveBeenLastCalledWith({
    kind: "human", id: "colleague", role: "member", listenMode: null,
  });
});

it("does not expose room management for a direct message", () => {
  state.activeRoom = { ...room, kind: "dm" };
  compose();
  expect(panel.onAddMember).toBeUndefined();
  expect(panel.onRenameRoom).toBeUndefined();
  expect(panel.onRemoveMember).toBeUndefined();
});

it("exposes only renaming for a group chat owner", async () => {
  state.activeRoom = { ...room, kind: "group-dm" };
  compose();
  expect(panel.onAddMember).toBeUndefined();
  expect(panel.onRemoveMember).toBeUndefined();
  await act(async () => panel.onRenameRoom?.("Project room"));
  expect(state.updateRoom).toHaveBeenCalledWith({ name: "Project room" });
});

it("does not expose member adds to a channel member without admin permission", () => {
  state.activeRoom = { ...room, kind: "channel", members: room.members.map((member) => member.id === "owner" ? { ...member, role: "member" } : member) };
  compose();
  expect(panel.onAddMember).toBeUndefined();
  expect(panel.onRenameRoom).toBeUndefined();
  expect(panel.onRemoveMember).toBeUndefined();
  expect(panel.onListenModeChange).toBeUndefined();
});

it.each(["channel", "dm", "group-dm"] as const)("lets room admins edit response modes in a %s", (kind) => {
  state.activeRoom = { ...room, kind, members: room.members.map((member) => member.id === "owner" ? { ...member, role: "admin" } : member) };
  compose();
  expect(panel.onListenModeChange).toBeTypeOf("function");
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

it("surfaces DM selection failures through the directory action error", async () => {
  state.selectDm.mockRejectedValueOnce(new Error("Conversation could not be opened."));
  compose();

  await act(async () => directory.onSelectParticipant(directory.participants[0]));

  await waitFor(() => expect(directory.creationError).toBe("Conversation could not be opened."));
});

it("openMessage selects the room and exposes targetMessageId on roomProps, clearing it after 3s", () => {
  vi.useFakeTimers();
  compose();
  act(() => { workspaceValue.openMessage("room-1", "msg-1"); });
  expect(state.selectRoom).toHaveBeenCalledWith("room-1");
  expect(panel.targetMessageId).toBe("msg-1");
  act(() => { vi.advanceTimersByTime(3000); });
  expect(panel.targetMessageId).toBeNull();
  vi.useRealTimers();
});

it("renders the empty conversation when no room is selected", () => {
  state.activeRoomId = null;
  compose();
  expect(screen.getByRole("region", { name: "No conversation selected" })).toBeInTheDocument();
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
