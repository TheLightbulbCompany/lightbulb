import { act, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { ApiError, type CanonicalRoomMessage, type RoomDetail } from "@lightbulb/shared";

vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ userId: "user_alice" }) }));
const rooms = vi.hoisted(() => ({ value: {} as Record<string, unknown> }));
vi.mock("@/hooks/useRooms", () => ({ useRooms: (...args: unknown[]) => { rooms.value.args = args; return rooms.value; } }));

import { SessionChat } from "../SessionChat";

const member = (id: string, displayName: string, role: "owner" | "member" = "member") =>
  ({ kind: "human" as const, id, displayName, mentionHandle: null, role, listenMode: null, joinedAt: "t", joinedSeq: 0 });
const detail: RoomDetail = {
  roomId: "chat-1", name: "Session chat · Fix relay", kind: "channel", roomRevision: 1, deliveryKey: "k",
  lastMessageSeq: 1, lastMessageAt: null, unreadCount: 0, updatedAt: "t", topic: null, starred: false, muted: false,
  sectionId: null, lastReadSeq: 0, createdAt: "t", pinnedMessageIds: [], session: { sessionId: "s1" },
  members: [member("user_alice", "Alice", "owner"), member("user_bob", "Bob")],
};
const fromBob: CanonicalRoomMessage = {
  messageId: "m1", roomId: "chat-1", messageSeq: 1, roomRevision: 1, author: { kind: "human", id: "user_bob", displayName: "Bob" },
  text: "is it the relay?", mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: "2026-10-07T10:00:00Z", runId: null,
};
const selectRoom = vi.fn();
beforeEach(() => {
  selectRoom.mockClear();
  rooms.value = {
    selectRoom, sendMessage: vi.fn(), activeRoomId: "chat-1", activeRoom: detail, messages: [fromBob],
    optimisticMessages: [], historyComplete: true, historyLoading: false, historyErrorMessage: null,
    loadOlder: vi.fn(), sendState: () => "idle", reconcileSend: vi.fn(), httpReady: true,
  };
});

it("opens the session's own room in a second slot and colours people as the terminal does", async () => {
  const onRoom = vi.fn();
  const view = render(<SessionChat sessionId="s1" sessionOwnerId="user_alice" openChat={vi.fn(async () => detail)} onClose={vi.fn()} onRoom={onRoom} />);
  await waitFor(() => expect(selectRoom).toHaveBeenCalledWith("chat-1"));
  expect(rooms.value.args).toEqual([null, true, true, false, false]); // its own slot: never auto-selects a listed room; reads while visible
  expect(onRoom).toHaveBeenCalledWith("chat-1");
  expect(screen.getByRole("complementary", { name: "Session chat" })).toBeInTheDocument();
  expect(document.querySelector('[data-person="green"]')).toHaveTextContent("is it the relay?");
  expect(screen.getByLabelText("Message the people here")).toBeInTheDocument();
  expect(screen.getByPlaceholderText("Message the people here")).toBeInTheDocument(); // prod showed the room's generic one
  view.unmount();
  expect(onRoom).toHaveBeenLastCalledWith(null);
});

it("reads an ended session's chat without a composer", async () => {
  rooms.value.activeRoom = { ...detail, archivedAt: "2026-10-07T11:00:00Z" };
  render(<SessionChat sessionId="s1" sessionOwnerId="user_alice" openChat={vi.fn(async () => detail)} onClose={vi.fn()} />);
  expect(await screen.findByText("This session ended. Its chat is read-only.")).toBeInTheDocument();
  expect(screen.queryByLabelText("Message the people here")).not.toBeInTheDocument();
});

it("says when the chat is full or unavailable", async () => {
  const { unmount } = render(<SessionChat sessionId="s1" sessionOwnerId="user_alice" openChat={vi.fn(async () => { throw new ApiError(409, null); })} onClose={vi.fn()} />);
  expect(await screen.findByText("This chat is full.")).toBeInTheDocument();
  unmount();
  render(<SessionChat sessionId="s1" sessionOwnerId="user_alice" openChat={vi.fn(async () => { throw new ApiError(404, null); })} onClose={vi.fn()} />);
  expect(await screen.findByText("Chat is not available for this session.")).toBeInTheDocument();
});

it("shows a message's attachments (the phone and agents can post them into the same room)", async () => {
  global.URL.createObjectURL = vi.fn(() => "blob:mock");
  global.URL.revokeObjectURL = vi.fn();
  const downloadAttachment = vi.fn(async () => new Blob(["x"], { type: "image/png" }));
  rooms.value = { ...rooms.value, downloadAttachment, attachmentUrl: vi.fn(),
    messages: [{ ...fromBob, attachments: [{ id: "att-1", fileName: "trace.png", contentType: "image/png", size: 10 }] }] };
  render(<SessionChat sessionId="s1" sessionOwnerId="user_alice" openChat={vi.fn(async () => detail)} onClose={vi.fn()} />);
  await waitFor(() => expect(downloadAttachment).toHaveBeenCalledWith("chat-1", "att-1"));
});

it("lets a person attach a file, as the phone's room screen does", async () => {
  rooms.value = { ...rooms.value, uploadAttachment: vi.fn() };
  render(<SessionChat sessionId="s1" sessionOwnerId="user_alice" openChat={vi.fn(async () => detail)} onClose={vi.fn()} />);
  await screen.findByLabelText("Message the people here");
  expect(document.querySelector('.session-chat input[type="file"]')).not.toBeNull();
});

it("says access was removed, with no composer, when sharing pauses while the chat is open", async () => {
  rooms.value = { ...rooms.value, accessRemoved: true, messages: [] };
  render(<SessionChat sessionId="s1" sessionOwnerId="user_alice" openChat={vi.fn(async () => detail)} onClose={vi.fn()} />);
  expect(await screen.findByText("Access removed. You can no longer view this conversation.")).toBeInTheDocument();
  expect(screen.queryByLabelText("Message the people here")).not.toBeInTheDocument();
});

it("closes from the left of its header, away from the session's own close (a double click closed both)", async () => {
  render(<SessionChat sessionId="s1" sessionOwnerId="user_alice" openChat={vi.fn(async () => detail)} onClose={vi.fn()} />);
  const close = await screen.findByRole("button", { name: "Close chat" });
  expect(close.parentElement).toHaveClass("room-context-header");
  expect(close.parentElement!.firstElementChild).toBe(close);
  expect(close).not.toHaveClass("ml-auto");
});

it("shows its frame at once and loads the room the session already names while the door opens", async () => {
  let land!: (room: RoomDetail) => void;
  const openChat = vi.fn(() => new Promise<RoomDetail>((resolve) => { land = resolve; }));
  rooms.value = { ...rooms.value, messages: [] };
  render(<SessionChat sessionId="s1" sessionOwnerId="user_alice" knownRoomId="chat-1" openChat={openChat} onClose={vi.fn()} />);
  expect(selectRoom).toHaveBeenCalledWith("chat-1"); // in parallel with the POST
  expect(screen.getByRole("heading", { name: "Chat" })).toBeInTheDocument();
  expect(screen.getByText("Opening chat…")).toBeInTheDocument();
  expect(screen.getByLabelText("Message the people here")).toBeDisabled(); // the door must open before anyone posts
  await act(async () => { land(detail); });
  expect(screen.getByLabelText("Message the people here")).toBeEnabled();
  expect(screen.queryByText("Opening chat…")).not.toBeInTheDocument();
});

it("shows the known room's messages before the door answers", () => {
  render(<SessionChat sessionId="s1" sessionOwnerId="user_alice" knownRoomId="chat-1" openChat={vi.fn(() => new Promise<RoomDetail>(() => {}))} onClose={vi.fn()} />);
  expect(screen.getByText("is it the relay?")).toBeInTheDocument();
  expect(screen.getByLabelText("Message the people here")).toBeDisabled();
});

it("says so each time its read cursor moves: on open and on a message read while it is open", async () => {
  const onRead = vi.fn();
  const props = { sessionId: "s1", sessionOwnerId: "user_alice", openChat: vi.fn(async () => detail), onClose: vi.fn(), onRead };
  const view = render(<SessionChat {...props} />);
  await waitFor(() => expect(onRead).toHaveBeenCalledTimes(1));
  rooms.value = { ...rooms.value, activeRoom: { ...detail, lastReadSeq: 2 } }; // markRead landed for a new arrival
  view.rerender(<SessionChat {...props} />);
  await waitFor(() => expect(onRead).toHaveBeenCalledTimes(2));
});

it("reports the known room as on screen before the door answers, and takes it back when the door refuses", async () => {
  const onRoom = vi.fn();
  let refuse!: (error: unknown) => void;
  const openChat = vi.fn(() => new Promise<RoomDetail>((_, reject) => { refuse = reject; }));
  render(<SessionChat sessionId="s1" sessionOwnerId="user_alice" knownRoomId="chat-1" openChat={openChat} onClose={vi.fn()} onRoom={onRoom} />);
  expect(onRoom).toHaveBeenLastCalledWith("chat-1"); // its messages are on screen: no toast for them
  await act(async () => { refuse(new ApiError(404, null)); });
  expect(await screen.findByText("Chat is not available for this session.")).toBeInTheDocument();
  expect(onRoom).toHaveBeenLastCalledWith(null);
  // The slot lets go of the room: nothing is fetched or marked read behind the error panel.
  expect(selectRoom).toHaveBeenLastCalledWith(null);
});

it("reads the known room again once the door answers (a session that ended during the race is not left writable)", async () => {
  const refreshRoom = vi.fn();
  rooms.value = { ...rooms.value, refreshRoom };
  let land!: (room: RoomDetail) => void;
  render(<SessionChat sessionId="s1" sessionOwnerId="user_alice" knownRoomId="chat-1" openChat={vi.fn(() => new Promise<RoomDetail>((resolve) => { land = resolve; }))} onClose={vi.fn()} />);
  expect(refreshRoom).not.toHaveBeenCalled();
  await act(async () => { land({ ...detail, archivedAt: "2026-10-07T11:00:00Z" }); });
  expect(refreshRoom).toHaveBeenCalledTimes(1);
});

it("stops counting the room as on screen once access to it is removed", async () => {
  const onRoom = vi.fn();
  const props = { sessionId: "s1", sessionOwnerId: "user_alice", openChat: vi.fn(async () => detail), onClose: vi.fn(), onRoom };
  const view = render(<SessionChat {...props} />);
  await waitFor(() => expect(onRoom).toHaveBeenLastCalledWith("chat-1"));
  rooms.value = { ...rooms.value, accessRemoved: true, messages: [] };
  view.rerender(<SessionChat {...props} />);
  await waitFor(() => expect(onRoom).toHaveBeenLastCalledWith(null));
});
