import { render, screen, fireEvent } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import type { RoomSummary } from "@lightbulb/shared";
import type { RoomParticipant } from "../types";

const voice = vi.hoisted(() => ({ active: false }));
const huddle = vi.hoisted(() => ({ join: vi.fn(), roomId: null as string | null, ids: [] as string[], available: true }));
vi.mock("@/components/voice/VoiceCallProvider", () => ({ useVoiceCall: () => ({ view: null, start: vi.fn(), ...voice }) }));
vi.mock("@/components/huddle/HuddleProvider", () => ({ useHuddle: () => ({ join: huddle.join, roomId: huddle.roomId }) }));
vi.mock("@/components/huddle/useRoomHuddle", () => ({ useRoomHuddle: () => ({ userIds: huddle.ids, available: huddle.available }) }));
import { RoomHeader } from "../RoomHeader";

const room = (extra: Partial<RoomSummary> = {}): RoomSummary => ({
  roomId: "room-1", name: null, kind: "channel", roomRevision: 1, lastMessageSeq: 0, lastMessageAt: null, unreadCount: 0,
  updatedAt: "2026-09-11T10:00:00Z", topic: null, starred: false, muted: false, sectionId: null, members: [], lastReadSeq: 0, ...extra,
} as RoomSummary);
const who = (kind: "human" | "agent", id: string, displayName: string, listenMode: RoomParticipant["listenMode"] = null): RoomParticipant => ({
  kind, id, displayName, mentionHandle: null, role: "member", listenMode, joinedAt: "2026-09-01T00:00:00Z", joinedSeq: 0,
});
const people = [who("human", "h1", "Ana"), who("human", "h2", "Ben"), who("human", "h3", "Cy"), who("agent", "a1", "CEO", "active")];
const base = { roomName: "general", participantCount: 4, canEditRoom: false, canLeave: false, actionable: true, pinnedCount: 0, search: <span />, onOpenDetails: vi.fn(), counterparts: people };

beforeEach(() => { voice.active = false; huddle.join.mockClear(); huddle.roomId = null; huddle.ids = []; huddle.available = true; });

it("Go live joins this room", () => {
  render(<RoomHeader {...base} room={room()} />);
  fireEvent.click(screen.getByRole("button", { name: "Go live" }));
  expect(huddle.join).toHaveBeenCalledWith("room-1", "general");
});

it("is hidden for guests and disabled during a call", () => {
  const { unmount } = render(<RoomHeader {...base} room={room({ guest: true } as Partial<RoomSummary>)} />);
  expect(screen.queryByRole("button", { name: "Go live" })).toBeNull();
  unmount();
  voice.active = true;
  render(<RoomHeader {...base} room={room()} />);
  expect(screen.getByRole("button", { name: "Go live" })).toBeDisabled();
});

it.each([
  [["h1"], "🔴 Live — Ana"],
  [["h1", "h2"], "🔴 Live — Ana and Ben"],
  [["h1", "h2", "h3"], "🔴 Live — Ana and 2 others"],
])("marker for %j", (ids, text) => {
  huddle.ids = ids;
  render(<RoomHeader {...base} room={room()} />);
  expect(screen.getByText(text)).toBeInTheDocument();
});

it("is hidden while Live is switched off", () => {
  huddle.available = false;
  render(<RoomHeader {...base} room={room()} />);
  expect(screen.queryByRole("button", { name: "Go live" })).toBeNull();
});
