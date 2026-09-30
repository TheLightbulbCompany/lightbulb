import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { RoomSummary } from "@lightbulb/shared";
import type { RoomParticipant } from "../types";

vi.mock("../VoiceCallButton", () => ({ VoiceCallButton: ({ agentName }: { agentName: string }) => <button>Call {agentName}</button> }));

import { RoomHeader } from "../RoomHeader";

const room: RoomSummary = {
  roomId: "room-1", name: null, kind: "dm", roomRevision: 1, lastMessageSeq: 0,
  lastMessageAt: null, unreadCount: 0, updatedAt: "2026-09-11T10:00:00Z",
  topic: null, starred: false, muted: false, sectionId: null, members: [], lastReadSeq: 0,
} as RoomSummary;
const who = (kind: "human" | "agent"): RoomParticipant => ({
  kind, id: "x1", displayName: "Finance", mentionHandle: null, role: "member",
  listenMode: null, joinedAt: "2026-09-01T00:00:00Z", joinedSeq: 0,
});
const base = { room, roomName: "Finance", participantCount: 2, canEditRoom: false, canLeave: false, actionable: true, pinnedCount: 0, search: <span />, onOpenDetails: vi.fn() };

it("shows Call in an agent DM only", () => {
  const { unmount } = render(<RoomHeader {...base} counterparts={[who("agent")]} />);
  expect(screen.getByRole("button", { name: "Call Finance" })).toBeInTheDocument();
  unmount();
  render(<RoomHeader {...base} counterparts={[who("human")]} />);
  expect(screen.queryByRole("button", { name: "Call Finance" })).not.toBeInTheDocument();
});
