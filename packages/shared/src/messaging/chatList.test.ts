import { describe, expect, it } from "vitest";

import {
  agentRingState, mergeChats, roomPreview, sessionPreview, sessionRingState,
  plainPreview,
} from "./chatList";
import type { LocalSession, RoomSummary } from "./types";

const room = (over: Partial<RoomSummary>): RoomSummary => ({
  roomId: "r", name: null, kind: "channel", roomRevision: 1, lastMessageSeq: 1,
  lastMessageAt: "2026-09-26T10:00:00Z", unreadCount: 0, updatedAt: "2026-09-26T10:00:00Z",
  topic: null, starred: false, muted: false, sectionId: null, members: [], lastReadSeq: 0, ...over,
});
const session = (over: Partial<LocalSession>): LocalSession => ({
  sessionId: "s", deviceId: "d", humanUserId: "u", provider: "claude", generation: "g",
  status: "idle", providerVersion: "2", controlUnavailableReason: null, activeTurnId: null,
  capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true,
  latestEventSeq: 5, lastEventAt: "2026-09-26T09:00:00Z", ...over,
});
const NOW = Date.parse("2026-09-26T12:00:00Z");

describe("mergeChats", () => {
  it("interleaves rooms and sessions newest first", () => {
    const entries = mergeChats(
      [room({ roomId: "a", lastMessageAt: "2026-09-26T08:00:00Z" }), room({ roomId: "b", lastMessageAt: "2026-09-26T11:00:00Z" })],
      [session({ sessionId: "s1", lastEventAt: "2026-09-26T10:00:00Z" })],
      { now: NOW },
    );
    expect(entries.map((e) => e.key)).toEqual(["room:b", "session:s1", "room:a"]);
  });

  it("falls back to updatedAt for a room and endedAt for a session", () => {
    const entries = mergeChats(
      [room({ roomId: "a", lastMessageAt: null, updatedAt: "2026-09-26T11:30:00Z" })],
      [session({ sessionId: "s1", lastEventAt: null, endedAt: "2026-09-26T11:00:00Z", status: "offline" })],
      { now: NOW },
    );
    expect(entries.map((e) => e.key)).toEqual(["room:a", "session:s1"]);
  });

  it("orders missing timestamps last, stably", () => {
    const entries = mergeChats(
      [room({ roomId: "z", lastMessageAt: null, updatedAt: "not a date" }), room({ roomId: "y", lastMessageAt: null, updatedAt: "" }), room({ roomId: "x" })],
      [session({ sessionId: "s1", lastEventAt: undefined, endedAt: undefined })],
      { now: NOW },
    );
    expect(entries.map((e) => e.key)).toEqual(["room:x", "room:y", "room:z", "session:s1"]);
    expect(entries.every((e) => Number.isFinite(e.at))).toBe(true);
  });

  it("keeps ended sessions as ordinary chats, and drops deleted rooms and sessions offline for three days", () => {
    const entries = mergeChats(
      [room({ roomId: "gone", deletedAt: "2026-09-26T09:00:00Z" })],
      [
        session({ sessionId: "ended", status: "ended", lastEventAt: "2026-09-26T08:00:00Z" }),
        session({ sessionId: "stale", status: "offline", lastEventAt: "2026-09-20T00:00:00Z" }),
        session({ sessionId: "live", status: "busy" }),
      ],
      { now: NOW },
    );
    expect(entries.map((e) => e.key)).toEqual(["session:live", "session:ended"]);
  });

  it("hides a session archived on this device until it has new output", () => {
    const quiet = session({ sessionId: "s1", latestEventSeq: 5 });
    expect(mergeChats([], [quiet], { archivedSessions: { s1: 5 }, now: NOW })).toEqual([]);
    const moved = session({ sessionId: "s1", latestEventSeq: 6 });
    expect(mergeChats([], [moved], { archivedSessions: { s1: 5 }, now: NOW }).map((e) => e.key)).toEqual(["session:s1"]);
  });
});

describe("roomPreview", () => {
  const last = { messageId: "m", seq: 3, authorId: "u2", authorKind: "human" as const, authorName: "Leo", textPreview: "can someone check the payout?", hasAttachments: false, createdAt: "x", threadRootMessageId: null };
  it("prefixes the author in a channel", () => {
    expect(roomPreview({ kind: "channel", lastMessage: last }, "me")).toBe("Leo: can someone check the payout?");
  });
  it("leaves a 1:1 DM's counterpart unprefixed", () => {
    expect(roomPreview({ kind: "dm", lastMessage: last }, "me")).toBe("can someone check the payout?");
  });
  it("says You for the viewer's own message, in any room", () => {
    expect(roomPreview({ kind: "dm", lastMessage: { ...last, authorId: "me" } }, "me")).toBe("You: can someone check the payout?");
  });
  it("names an attachment-only message", () => {
    expect(roomPreview({ kind: "channel", lastMessage: { ...last, textPreview: " ", hasAttachments: true } }, "me")).toBe("Leo: Attachment");
  });
  it("has copy for an empty room", () => {
    expect(roomPreview({ kind: "channel", lastMessage: null }, "me")).toBe("No messages yet");
  });
});

describe("sessionPreview", () => {
  it("shows the last reply, else the status word", () => {
    expect(sessionPreview({ status: "idle", lastAssistantMessage: " All green. " })).toBe("All green.");
    expect(sessionPreview({ status: "offline", lastAssistantMessage: null })).toBe("Offline");
  });
  it("puts a waiting prompt ahead of the last reply", () => {
    expect(sessionPreview({ status: "waiting-approval", lastAssistantMessage: "Done" })).toBe("Waiting for input");
    expect(sessionPreview({ status: "waiting-approval", waitingFor: "permission prompt", lastAssistantMessage: "Done" })).toBe("Waiting for input: permission prompt");
    expect(sessionPreview({ status: "idle", note: "run lightbulb login", lastAssistantMessage: "Done" })).toBe("Done"); // a hand-off note is never shown
  });
});

describe("ring states", () => {
  it("rings only real states; a never-run 'active' agent is not working", () => {
    expect(agentRingState("running")).toBe("running");
    expect(agentRingState("Thinking")).toBe("running");
    expect(agentRingState("stalled")).toBe("running");
    expect(agentRingState("error")).toBe("attention");
    expect(agentRingState("failed")).toBe("attention");
    expect(agentRingState("paused")).toBe("paused");
    expect(agentRingState("active")).toBeNull();
    expect(agentRingState("idle")).toBeNull();
    expect(agentRingState(undefined)).toBeNull();
  });
  it("rings a busy session live and a waiting one for attention", () => {
    expect(sessionRingState({ status: "busy" })).toBe("running");
    expect(sessionRingState({ status: "waiting-approval" })).toBe("attention");
    expect(sessionRingState({ status: "idle" })).toBeNull();
    expect(sessionRingState({ status: "idle", note: "press Resume" })).toBeNull();
  });
});

describe("plainPreview", () => {
  it("keeps double underscores, which are usually identifiers", () => {
    expect(plainPreview("edit __init__.py")).toBe("edit __init__.py");
    // The composer escapes what was typed; the preview shows it as typed.
    expect(plainPreview("\\_\\_init\\_\\_ and 3\\*4, \\*\\*not bold\\*\\*")).toBe("__init__ and 3*4, **not bold**");
  });
  it("drops markdown markers and collapses lines", () => {
    expect(plainPreview("Where the two PRs stand:\n\n**#3379, the cleanup:** every `review`\n- one")).toBe(
      "Where the two PRs stand: #3379, the cleanup: every review one",
    );
  });
});
