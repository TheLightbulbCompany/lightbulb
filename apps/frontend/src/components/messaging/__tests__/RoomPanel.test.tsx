import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { RoomPanel, type RoomPanelProps } from "../RoomPanel";
import type { RoomMessage, RoomParticipant } from "../types";

const owner: RoomParticipant = { kind: "human", id: "owner", displayName: "Owner", mentionHandle: null, role: "owner", listenMode: null, joinedAt: "2026-09-10T00:00:00Z", joinedSeq: 0 };
const friday: RoomParticipant = { ...owner, kind: "agent", id: "friday", displayName: "Friday", role: "member", agentActivity: "running" };
const message: RoomMessage = { messageId: "message", roomId: "room", messageSeq: 1, roomRevision: 1, author: { kind: "agent", id: "friday", displayName: "Old name" }, text: "Hello", mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: "2026-09-10T00:00:00Z", runId: null, run: { state: "completed" } };
const props: RoomPanelProps = {
  ownerScopeId: "org", currentUserId: "owner", roomId: "room", state: "ready", httpReady: true, liveConnected: true,
  room: { roomId: "room", kind: "dm", name: null, roomRevision: 1, lastMessageSeq: 1, lastMessageAt: null, unreadCount: 0, updatedAt: "2026-09-10T00:00:00Z" },
  participants: [owner, friday], messages: [message],
  composer: { text: "", onTextChange: vi.fn(), members: [owner, friday], selectedMentions: [], onMentionsChange: vi.fn(), clientMessageId: "draft", sendState: "idle", onSubmit: vi.fn() },
};

it("names a DM for its counterpart and uses canonical author and member activity", () => {
  render(<RoomPanel {...props} />);
  expect(screen.getByRole("region", { name: "Friday" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Friday" })).toBeInTheDocument();
  const article = screen.getByRole("article", { name: "Message from Friday" });
  expect(within(article).getByRole("img", { name: "Friday" }).parentElement).toHaveAttribute("data-agent-state", "running");
  expect(within(article).getByText("Agent")).toBeInTheDocument();
  expect(within(article).getByRole("status", { name: "Work status" })).toHaveTextContent("Completed");
  fireEvent.click(screen.getByRole("button", { name: "View members" }));
  const members = screen.getByRole("region", { name: "Room members" });
  expect(within(members).getByRole("img", { name: "Friday" }).parentElement).toHaveAttribute("data-agent-state", "running");
  expect(within(members).queryByRole("status", { name: "Work status" })).not.toBeInTheDocument();
});

it("excludes only the signed-in human when naming a group DM", () => {
  render(<RoomPanel {...props} room={{ ...props.room!, kind: "group-dm" }} participants={[owner, { ...owner, id: "alice", displayName: "Alice" }, { ...friday, id: "owner", displayName: "Atlas" }]} messages={[]} />);
  expect(screen.getByRole("heading", { name: "Alice and Atlas" })).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Alice and Atlas" })).toBeInTheDocument();
});

it.each(["system", "human"] as const)("keeps unmatched %s authors neutral", (kind) => {
  render(<RoomPanel {...props} messages={[{ ...message, author: { kind, id: "friday", displayName: "Announcement" } }]} />);
  const article = screen.getByRole("article", { name: "Message from Announcement" });
  expect(within(article).getByRole("img", { name: "Announcement" }).parentElement).not.toHaveAttribute("data-agent-state");
  expect(within(article).queryByText(/^Agent /)).not.toBeInTheDocument();
});
