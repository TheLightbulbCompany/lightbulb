import type { LocalSession, RoomMember, RoomSummary } from "@lightbulb/shared";

import {
  archiveSession, buildPinnedItems, movePin, parsePins, pinKeyForRoom, readArchivedSessions, readPins,
  readShowRunningSessions, resolvePins, togglePin, writePins, writeShowRunningSessions, type PinKey,
} from "../chats";

const member = (over: Partial<RoomMember>): RoomMember => ({
  kind: "human", id: "me", displayName: "Me", mentionHandle: null, role: "member", listenMode: null, joinedAt: "x", joinedSeq: 0, ...over,
});
const room = (over: Partial<RoomSummary>): RoomSummary => ({
  roomId: "r", name: null, kind: "channel", roomRevision: 1, lastMessageSeq: 1, lastMessageAt: null, unreadCount: 0,
  updatedAt: "2026-09-26T10:00:00Z", topic: null, starred: false, muted: false, sectionId: null, members: [], lastReadSeq: 0, ...over,
});
const session = (over: Partial<LocalSession>): LocalSession => ({
  sessionId: "s", deviceId: "d", humanUserId: "me", provider: "claude", generation: "g", status: "busy",
  providerVersion: "2", controlUnavailableReason: null, activeTurnId: null,
  capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true, latestEventSeq: 3, ...over,
});

describe("parsePins", () => {
  it("accepts a valid list and rejects malformed shapes", () => {
    expect(parsePins(["agent:ok", "room:r1"])).toEqual(["agent:ok", "room:r1"]);
    expect(parsePins(["agent:ok", 7])).toBeNull();
    expect(parsePins(["channel:x"])).toBeNull();
    expect(parsePins("agent:x")).toBeNull();
  });
});

describe("pins storage", () => {
  it("is null until edited, then round-trips without duplicates", () => {
    expect(readPins("org-a")).toBeNull();
    writePins("org-a", ["agent:ceo", "room:r1", "agent:ceo"]);
    expect(readPins("org-a")).toEqual(["agent:ceo", "room:r1"]);
  });

  it("scopes by org", () => {
    writePins("org-b", ["agent:x"]);
    expect(readPins("org-c")).toBeNull();
  });

  it("uses the starter agents in roster order until the reader edits", () => {
    expect(resolvePins(null, ["ceo", "eng"])).toEqual(["agent:ceo", "agent:eng"]);
    expect(resolvePins(null, [])).toEqual([]);
    expect(resolvePins(["room:r1"], ["ceo"])).toEqual(["room:r1"]);
  });

  it("toggles and moves", () => {
    const pins: PinKey[] = ["agent:a", "agent:b", "agent:c"];
    expect(togglePin(pins, "agent:b")).toEqual(["agent:a", "agent:c"]);
    expect(togglePin(pins, "room:z")).toEqual([...pins, "room:z"]);
    expect(movePin(pins, "agent:b", -1)).toEqual(["agent:b", "agent:a", "agent:c"]);
    expect(movePin(pins, "agent:c", 1)).toEqual(pins);
    expect(movePin(pins, "agent:zz", 1)).toEqual(pins);
  });
});

describe("running-sessions switch", () => {
  it("defaults on and remembers off", () => {
    expect(readShowRunningSessions()).toBe(true);
    writeShowRunningSessions(false);
    expect(readShowRunningSessions()).toBe(false);
    writeShowRunningSessions(true);
    expect(readShowRunningSessions()).toBe(true);
  });
});

describe("archived sessions", () => {
  it("records the seq a session had when archived, per org", () => {
    archiveSession("org-e", { sessionId: "s1", latestEventSeq: 9 });
    expect(readArchivedSessions("org-e")).toEqual({ s1: 9 });
    expect(readArchivedSessions("org-f")).toEqual({});
  });
});

describe("pinKeyForRoom", () => {
  it("pins a one-agent DM as the agent and everything else as the room", () => {
    expect(pinKeyForRoom({ roomId: "d1", kind: "dm" }, [{ kind: "agent", id: "ceo" }])).toBe("agent:ceo");
    expect(pinKeyForRoom({ roomId: "d2", kind: "dm" }, [{ kind: "human", id: "leo" }])).toBe("room:d2");
    expect(pinKeyForRoom({ roomId: "c1", kind: "channel" }, [])).toBe("room:c1");
  });
});

describe("buildPinnedItems", () => {
  const dm = room({ roomId: "dm-ceo", kind: "dm", members: [member({}), member({ kind: "agent", id: "ceo", displayName: "CEO" })] });
  const general = room({ roomId: "gen", name: "general" });

  it("resolves agents (with their DM and ring) and rooms, in pin order", () => {
    const items = buildPinnedItems({
      pins: ["room:gen", "agent:ceo"],
      rooms: [dm, general],
      agents: [{ id: "ceo", name: "CEO", status: "running" }],
      sessions: [], showRunningSessions: true, viewerId: "me",
    });
    expect(items.map((i) => i.key)).toEqual(["room:gen", "agent:ceo"]);
    expect(items[1]).toMatchObject({ kind: "agent", name: "CEO", ring: "running", roomId: "dm-ceo" });
  });

  it("drops unknown targets", () => {
    const items = buildPinnedItems({
      pins: ["agent:gone", "room:left"], rooms: [general], agents: [], sessions: [], showRunningSessions: true, viewerId: "me",
    });
    expect(items).toEqual([]);
  });

  it("puts busy sessions first when the switch is on, and none when off", () => {
    const busy = session({ sessionId: "s1", status: "busy" });
    const idle = session({ sessionId: "s2", status: "idle" });
    const on = buildPinnedItems({ pins: ["room:gen"], rooms: [general], agents: [], sessions: [idle, busy], showRunningSessions: true, viewerId: "me" });
    expect(on.map((i) => i.key)).toEqual(["session:s1", "room:gen"]);
    // The pinned tile already shows the tool's logo, so the name is the
    // session's own title (falling back to its id), not "Claude" again.
    expect(on[0]).toMatchObject({ kind: "session", name: "s1" });
    const off = buildPinnedItems({ pins: ["room:gen"], rooms: [general], agents: [], sessions: [busy], showRunningSessions: false, viewerId: "me" });
    expect(off.map((i) => i.key)).toEqual(["room:gen"]);
  });

  it("names an agent from its DM when the roster has not answered yet", () => {
    const items = buildPinnedItems({ pins: ["agent:ceo"], rooms: [dm], agents: [], sessions: [], showRunningSessions: false, viewerId: "me" });
    expect(items[0]).toMatchObject({ name: "CEO", ring: null });
  });
});
