import { render, screen, waitFor } from "@testing-library/react";
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
  expect(rooms.value.args).toEqual([null, true, true, false]); // its own slot: never auto-selects a listed room
  expect(onRoom).toHaveBeenCalledWith("chat-1");
  expect(screen.getByRole("complementary", { name: "Session chat" })).toBeInTheDocument();
  expect(document.querySelector('[data-person="green"]')).toHaveTextContent("is it the relay?");
  expect(screen.getByLabelText("Message the people here")).toBeInTheDocument();
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
