// The conversation's tools and side column in the shell's one right pane, plus the one card
// rule only RoomPanel can apply: whether the card's author is still in the room.
import { render, screen } from "@testing-library/react";
import { beforeAll, beforeEach, expect, it, vi } from "vitest";
import { RoomPanel, type RoomPanelProps } from "../RoomPanel";
import type { RoomMessage, RoomParticipant } from "../types";
import { _resetControllerForTests } from "@/components/workspace/workspaceController";

const shared = vi.hoisted(() => ({ statusLoaded: true }));
vi.mock("@lightbulb/shared", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useStatusLoaded: () => shared.statusLoaded,
}));
const teamsRead = vi.hoisted(() => ({ approvals: {} as Record<string, unknown> }));
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string) => ({ data: teamsRead.approvals[path], mutate: vi.fn(), isLoading: false }),
    post: vi.fn(), mutate: vi.fn(),
  }),
}));
vi.mock("../useMessageSearch", () => ({ useMessageSearch: () => ({ hits: [], state: "idle" }) }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("@/components/huddle/useRoomHuddle", () => ({ useRoomHuddle: () => ({ userIds: [], available: false }) }));
vi.mock("@/lib/shellNav", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  pushSearchParams: vi.fn(),
  replaceSearchParams: vi.fn(),
}));

const owner: RoomParticipant = { kind: "human", id: "owner", displayName: "Owner", mentionHandle: null, role: "owner", listenMode: null, joinedAt: "2026-09-10T00:00:00Z", joinedSeq: 0 };
const friday: RoomParticipant = { ...owner, kind: "agent", id: "friday", displayName: "Friday", role: "member" };
const message: RoomMessage = { messageId: "message", roomId: "room", messageSeq: 1, roomRevision: 1, author: { kind: "agent", id: "friday", displayName: "Friday" }, text: "Hello", mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: "2026-09-10T00:00:00Z", runId: null };
const reply: RoomMessage = { ...message, messageId: "reply", messageSeq: 2, text: "Reply text", threadRootMessageId: "message" };
const props: RoomPanelProps = {
  ownerScopeId: "org", currentUserId: "owner", roomId: "room", state: "ready", httpReady: true, liveConnected: true,
  room: { roomId: "room", kind: "dm", name: null, roomRevision: 1, lastMessageSeq: 2, lastMessageAt: null, unreadCount: 0, updatedAt: "2026-09-10T00:00:00Z", topic: null, starred: false, muted: false, sectionId: null, members: [], lastReadSeq: 0 },
  participants: [owner, friday], messages: [message, reply],
  composer: { text: "", onTextChange: vi.fn(), members: [owner, friday], clientMessageId: "draft", sendState: "idle", onSubmit: vi.fn(), focusAgentId: "friday" },
};

// The card is a lazy chunk; warm it so the first mount fits findByRole's 1 s budget.
beforeAll(() => import("../openui/OpenUICard"));

beforeEach(() => {
  _resetControllerForTests();
  shared.statusLoaded = true;
});


it("a card from an agent no longer in the room is read-only; a member's card still answers", async () => {
  const card: RoomMessage = { ...message, text: "```openui-lang\nroot = Stack([b])\nb = Buttons([Button(\"Studio\"), Button(\"Starter\")])\n```" };
  const gone = render(<RoomPanel {...props} participants={[owner]} messages={[card]} onCardReply={vi.fn()} />);
  expect(await screen.findByRole("button", { name: "Studio" })).toBeDisabled();
  gone.unmount();
  render(<RoomPanel {...props} messages={[card]} onCardReply={vi.fn()} />);
  expect(await screen.findByRole("button", { name: "Studio" })).not.toBeDisabled();
});
