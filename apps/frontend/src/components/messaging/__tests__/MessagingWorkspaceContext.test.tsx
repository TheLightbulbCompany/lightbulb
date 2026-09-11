import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { RoomDetail } from "@lightbulb/shared";
import type { RoomDirectoryProps } from "../RoomDirectory";
import type { RoomPanelProps } from "../RoomPanel";
import { RoomComposer } from "../RoomComposer";
import { requestFocusComposer, _resetFocusComposerRequestForTests } from "@/lib/focusComposerRequest";
import type { LiveRoomRun } from "@/hooks/useRooms";

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
vi.mock("../RoomPanel", () => ({ RoomPanel: (props: RoomPanelProps) => {
  panel = props;
  return <section aria-label="Messages">{renderComposer && <RoomComposer {...props.composer} />}</section>;
} }));

import { MessagingWorkspaceProvider } from "../MessagingWorkspaceContext";
import { MessagingConversation, MessagingDirectory } from "../MessagingWorkspace";

const room: RoomDetail = {
  roomId: "room-1", kind: "dm", name: null, roomRevision: 1,
  deliveryKey: "delivery", lastMessageSeq: 0, lastMessageAt: null,
  unreadCount: 0, lastReadSeq: 0, createdAt: "2026-09-10T00:00:00Z", updatedAt: "2026-09-10T00:00:00Z",
  members: [
    { kind: "human", id: "owner", displayName: "Owner", mentionHandle: null, role: "owner", listenMode: null, joinedAt: "2026-09-10T00:00:00Z", joinedSeq: 0 },
    { kind: "agent", id: "agent-friday", displayName: "Friday", mentionHandle: "friday", role: "member", listenMode: "mentions", joinedAt: "2026-09-10T00:00:00Z", joinedSeq: 0 },
  ],
};
const state = {
  rooms: [room], roomsError: undefined, roomsLoading: false,
  directoryError: undefined as Error | undefined, directoryLoading: false,
  activeRoomId: room.roomId as string | null, activeRoom: room,
  roomError: undefined, roomLoading: false, accessRemoved: false,
  messages: [], optimisticMessages: [], typingMembers: [], runs: [] as LiveRoomRun[],
  liveConnected: true, httpReady: true, selectRoom: vi.fn(), selectDm: vi.fn(), selectAgentDm: vi.fn(),
  directoryRoomDetails: new Map([[room.roomId, room]]),
  refreshRooms: vi.fn(), refreshRoom: vi.fn(), createRoom: vi.fn(), updateRoom: vi.fn(),
  upsertMember: vi.fn(), removeMember: vi.fn(), sendMessage: vi.fn(), reconcileSend: vi.fn(),
  sendState: () => "idle", setTyping: vi.fn(), abortRun: vi.fn(), steerRun: vi.fn(),
};
beforeEach(() => {
  vi.clearAllMocks();
  state.activeRoomId = room.roomId;
  state.activeRoom = room;
  state.directoryError = undefined;
  state.directoryLoading = false;
  state.accessRemoved = false;
  state.runs = [];
  state.selectDm.mockResolvedValue(null);
  renderComposer = false;
  _resetFocusComposerRequestForTests();
  mocks.rooms.mockReturnValue(state);
  mocks.agents.mockReturnValue({ agents: [{ id: "agent-friday", name: "Friday" }] });
  mocks.roster.mockReturnValue([{ agent: { id: "agent-friday" }, state: "running" }]);
});
afterEach(cleanup);

function compose(onOpenRoom?: (room?: import("../types").DirectoryRoom) => void) {
  return render(<MessagingWorkspaceProvider initialAgentId="agent-friday" subscribeToRoom>
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

it("routes message authors through the existing participant DM action", async () => {
  compose();
  await act(async () => panel.onSelectParticipant?.(panel.participants[1]));
  expect(state.selectDm).toHaveBeenCalledWith(panel.participants[1]);
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

it("does not expose member management for a dm", () => {
  state.activeRoom = { ...room, kind: "dm" };
  compose();
  expect(panel.onAddMember).toBeUndefined();
  expect(panel.onRenameRoom).toBeUndefined();
  expect(panel.onRemoveMember).toBeUndefined();
});

it("does not expose member adds to a channel member without admin permission", () => {
  state.activeRoom = { ...room, kind: "channel", members: room.members.map((member) => member.id === "owner" ? { ...member, role: "member" } : member) };
  compose();
  expect(panel.onAddMember).toBeUndefined();
  expect(panel.onRenameRoom).toBeUndefined();
  expect(panel.onRemoveMember).toBeUndefined();
});

it.each([
  ["channel", "owner"], ["channel", "admin"], ["group-dm", "owner"], ["group-dm", "admin"],
] as const)("connects %s %s management to existing commands", async (kind, role) => {
  state.activeRoom = { ...room, kind, members: room.members.map((member) => member.id === "owner" ? { ...member, role } : member) };
  compose();
  await act(async () => panel.onRenameRoom?.("New name"));
  await act(async () => panel.onRemoveMember?.({ kind: "agent", id: "agent-friday" }));
  expect(state.updateRoom).toHaveBeenCalledWith({ name: "New name" });
  expect(state.removeMember).toHaveBeenCalledWith({ kind: "agent", id: "agent-friday" });
});

it("surfaces directory hydration failures through the existing retry control while retaining available rooms", () => {
  state.directoryError = new Error("Directory hydration failed");
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

it("clears the old room's drafts, mentions, and attempt IDs on room change", () => {
  const view = compose();
  act(() => {
    panel.composer.onTextChange("Old draft");
    panel.composer.onMentionsChange([{ kind: "agent", id: "agent-friday" }]);
    panel.onSelectThread?.("message-1");
  });
  act(() => {
    panel.threadComposer!.composer.onTextChange("Old reply");
    panel.threadComposer!.composer.onMentionsChange([{ kind: "agent", id: "agent-friday" }]);
  });
  const oldMessageId = panel.composer.clientMessageId;
  const oldThreadId = panel.threadComposer!.composer.clientMessageId;
  state.activeRoomId = "room-2";
  state.activeRoom = { ...room, roomId: "room-2" };
  view.rerender(<MessagingWorkspaceProvider initialAgentId="agent-friday" subscribeToRoom>
    <MessagingDirectory /><MessagingConversation />
  </MessagingWorkspaceProvider>);
  expect(panel.composer.text).toBe("");
  expect(panel.composer.selectedMentions).toEqual([]);
  expect(panel.composer.clientMessageId).not.toBe(oldMessageId);
  expect(panel.threadComposer).toBeUndefined();
  act(() => panel.onSelectThread?.("message-2"));
  expect(panel.threadComposer!.composer.text).toBe("");
  expect(panel.threadComposer!.composer.selectedMentions).toEqual([]);
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
