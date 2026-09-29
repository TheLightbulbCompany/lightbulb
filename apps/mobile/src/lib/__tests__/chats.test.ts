import { archiveSession, otherMembers, readArchivedSessions } from "../chats";

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
