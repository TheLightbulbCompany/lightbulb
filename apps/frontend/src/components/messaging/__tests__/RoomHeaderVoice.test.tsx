import { render, screen, fireEvent } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import type { RoomSummary } from "@lightbulb/shared";
import type { RoomParticipant } from "../types";

const voice = vi.hoisted(() => ({ active: false, start: vi.fn() }));
vi.mock("@/components/voice/VoiceCallProvider", () => ({ useVoiceCall: () => ({ view: null, ...voice }) }));
vi.mock("@/components/huddle/useRoomHuddle", () => ({ useRoomHuddle: () => ({ userIds: [], available: false }) }));

import { RoomHeader } from "../RoomHeader";

const room = (kind: "dm" | "channel"): RoomSummary => ({
  roomId: "room-1", name: null, kind, roomRevision: 1, lastMessageSeq: 0,
  lastMessageAt: null, unreadCount: 0, updatedAt: "2026-09-11T10:00:00Z",
  topic: null, starred: false, muted: false, sectionId: null, members: [], lastReadSeq: 0,
} as RoomSummary);
const who = (kind: "human" | "agent", id = "x1", listenMode: RoomParticipant["listenMode"] = null, displayName = "Finance"): RoomParticipant => ({
  kind, id, displayName, mentionHandle: null, role: "member",
  listenMode, joinedAt: "2026-09-01T00:00:00Z", joinedSeq: 0,
});
const base = { roomName: "Finance", participantCount: 2, canEditRoom: false, canLeave: false, actionable: true, pinnedCount: 0, search: <span />, onOpenDetails: vi.fn() };

beforeEach(() => { voice.active = false; voice.start.mockClear(); });

it("shows Talk in an agent DM and starts a call with that agent", () => {
  render(<RoomHeader {...base} room={room("dm")} counterparts={[who("agent")]} />);
  fireEvent.click(screen.getByRole("button", { name: "Talk to Finance" }));
  expect(voice.start).toHaveBeenCalledWith({ agentId: "x1", agentName: "Finance" });
});

it("shows no button in a human DM", () => {
  render(<RoomHeader {...base} room={room("dm")} counterparts={[who("human")]} />);
  expect(screen.queryByRole("button", { name: /Talk/ })).not.toBeInTheDocument();
});

it("shows no button in a channel of humans or observe-only agents", () => {
  render(<RoomHeader {...base} room={room("channel")} counterparts={[who("human", "h1"), who("agent", "a1", "observe")]} />);
  expect(screen.queryByRole("button", { name: /Talk/ })).not.toBeInTheDocument();
});

it("disables Talk while a call is live", () => {
  voice.active = true;
  render(<RoomHeader {...base} room={room("dm")} counterparts={[who("agent")]} />);
  expect(screen.getByRole("button", { name: "Talk to Finance" })).toBeDisabled();
});

it("never offers a private team call in a channel", () => {
  render(<RoomHeader {...base} room={room("channel")} counterparts={[who("agent", "a1", "active")]} />);
  expect(screen.queryByRole("button", { name: "Talk to the team" })).not.toBeInTheDocument();
});

it("hides voice buttons from a shared-channel guest", () => {
  render(<RoomHeader {...base} room={{ ...room("channel"), guest: true }} counterparts={[who("agent", "a1", "active")]} />);
  expect(screen.queryByRole("button", { name: /Talk/ })).not.toBeInTheDocument();
});
