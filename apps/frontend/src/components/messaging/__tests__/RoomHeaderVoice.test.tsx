import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { RoomSummary } from "@lightbulb/shared";
import type { RoomParticipant } from "../types";

vi.mock("@/components/huddle/useRoomHuddle", () => ({ useRoomHuddle: () => ({ userIds: [], available: true }) }));

import { RoomHeader } from "../RoomHeader";

const room = (kind: "dm" | "group-dm" | "channel"): RoomSummary => ({
  roomId: "room-1", name: null, kind, roomRevision: 1, lastMessageSeq: 0,
  lastMessageAt: null, unreadCount: 0, updatedAt: "2026-09-11T10:00:00Z",
  topic: null, starred: false, muted: false, sectionId: null, members: [], lastReadSeq: 0,
} as RoomSummary);
const agent: RoomParticipant = {
  kind: "agent", id: "x1", displayName: "Finance", mentionHandle: null, role: "member",
  listenMode: "active", joinedAt: "2026-09-01T00:00:00Z", joinedSeq: 0,
};
const base = { roomName: "Finance", participantCount: 2, canEditRoom: false, canLeave: false, actionable: true, pinnedCount: 0, onOpenDetails: vi.fn() };

it.each(["dm", "group-dm", "channel"] as const)("web has no Talk button, only Go live (%s)", (kind) => {
  render(<RoomHeader {...base} room={room(kind)} counterparts={[agent]} />);
  expect(screen.queryByRole("button", { name: /Talk/ })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Go live" })).toBeInTheDocument();
});

it("one-to-one (the Lightbulb page's DM): no People facepile or menu rows, and no Add people", async () => {
  const { default: userEvent } = await import("@testing-library/user-event");
  const { rerender } = render(<RoomHeader {...base} room={room("dm")} counterparts={[agent]} onAddPeople={vi.fn()} />);
  expect(screen.getByRole("button", { name: "People, 2 here" })).toBeInTheDocument();
  rerender(<RoomHeader {...base} room={room("dm")} counterparts={[agent]} oneToOne />);
  expect(screen.queryByRole("button", { name: /^People/ })).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "Finance" }));
  expect(screen.getByRole("menuitem", { name: "Details" })).toBeInTheDocument();
  expect(screen.queryByRole("menuitem", { name: "People" })).toBeNull();
  expect(screen.queryByRole("menuitem", { name: "Add people" })).toBeNull();
});
