import type { ChatEntry, RoomSummary } from "@lightbulb/shared";
import { archiveSession, fileSpecialists, otherMembers, readArchivedSessions } from "../chats";

describe("archived sessions", () => {
  it("records the seq a session had when archived, per org", () => {
    archiveSession("org-e", { sessionId: "s1", latestEventSeq: 9 });
    expect(readArchivedSessions("org-e")).toEqual({ s1: 9 });
    expect(readArchivedSessions("org-f")).toEqual({});
  });
});

describe("otherMembers", () => {
  it("names a conversation after everyone but the viewer", () => {
    const members = [
      { kind: "human" as const, id: "me", displayName: "Me" },
      { kind: "agent" as const, id: "me", displayName: "An agent that shares the id" },
      { kind: "human" as const, id: "ann", displayName: "Ann" },
    ];
    expect(otherMembers({ members } as never, "me").map((m) => m.displayName)).toEqual([
      "An agent that shares the id",
      "Ann",
    ]);
    expect(otherMembers(undefined, "me")).toEqual([]);
  });
});

describe("fileSpecialists (super-agent one voice)", () => {
  const member = (kind: "human" | "agent", id: string) => ({ kind, id, displayName: id, mentionHandle: null, role: "member", listenMode: null, joinedAt: "x", joinedSeq: 0 });
  const dm = (roomId: string, agent: string, kind: RoomSummary["kind"] = "dm") =>
    ({ kind: "room", key: roomId, room: { roomId, kind, members: [member("human", "me"), member("agent", agent)] } }) as unknown as ChatEntry;
  const entries = [dm("lb", "agent-lb"), dm("fin", "agent-fin"), dm("grp", "agent-eng", "group-dm")];
  const keys = (rows: ReturnType<typeof fileSpecialists>) => rows.map((r) => r.key);

  it("files specialist DMs under one closed Specialists row at the end", () => {
    expect(keys(fileSpecialists(entries, "agent-lb", "me", false))).toEqual(["lb", "grp", "specialists"]);
    expect(fileSpecialists(entries, "agent-lb", "me", false).at(-1)).toMatchObject({ kind: "specialists", count: 1, open: false });
  });
  it("lists them when open", () => {
    expect(keys(fileSpecialists(entries, "agent-lb", "me", true))).toEqual(["lb", "grp", "specialists", "fin"]);
  });
  it("no front agent known, or no specialist DMs: the entries as they are", () => {
    expect(fileSpecialists(entries, undefined, "me", false)).toBe(entries);
    expect(fileSpecialists([entries[0]], "agent-lb", "me", false)).toEqual([entries[0]]);
  });
});
