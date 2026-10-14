import type { RoomSummary } from "@lightbulb/shared";
import { findCeoDm } from "./phoneHome";

const room = (roomId: string, kind: RoomSummary["kind"], memberId: string, displayName: string): RoomSummary => ({
  roomId,
  name: kind === "dm" ? null : displayName,
  kind,
  roomRevision: 1,
  lastMessageSeq: 0,
  lastMessageAt: null,
  unreadCount: 0,
  updatedAt: "2026-10-14T00:00:00Z",
  topic: null,
  starred: false,
  muted: false,
  sectionId: null,
  members: [{
    kind: "agent",
    id: memberId,
    displayName,
    mentionHandle: null,
    role: "member",
    listenMode: "active",
    joinedAt: "2026-10-14T00:00:00Z",
    joinedSeq: 1,
  }],
  lastReadSeq: 0,
});

it("selects the CEO DM instead of a workspace room", () => {
  const rooms = [room("general", "channel", "agent-other", "General"), room("ceo-room", "dm", "agent-ceo", "Renamed CEO")];
  expect(findCeoDm(rooms, "user-owner", "agent-ceo")?.roomId).toBe("ceo-room");
});

it("does not mistake an editable display name for the CEO identity", () => {
  expect(findCeoDm([room("friday-room", "dm", "agent-marketplace", "CEO")], "user-owner", "agent-ceo")).toBeNull();
});