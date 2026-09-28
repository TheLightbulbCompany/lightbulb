import { describe, expect, it } from "vitest";

import { lastOwnMessageId, readState, readStateLabel, unpromptedLabel } from "./readState";
import type { RoomReceipts } from "./useRooms";
import type { CanonicalRoomMessage } from "./types";

type M = Pick<CanonicalRoomMessage, "messageId" | "messageSeq" | "author" | "runId" | "deletedAt" | "mentions" | "addressedAgentIds">;
const msg = (
  seq: number, kind: "human" | "agent" | "system", id: string, runId: string | null = null, deletedAt: string | null = null,
  mentions: NonNullable<M["mentions"]> = [], addressedAgentIds?: string[],
): M => ({
  messageId: `m${seq}`, messageSeq: seq, author: { kind, id, displayName: id }, runId, deletedAt, mentions, addressedAgentIds,
});
const receipts = (over: Partial<{ reads: [string, number][]; deliveries: [string, number][]; agentReads: [string, string[]][] }> = {}): RoomReceipts => ({
  readCursors: new Map(over.reads ?? []),
  agentDeliveries: new Map(over.deliveries ?? []),
  agentReads: new Map((over.agentReads ?? []).map(([agent, ids]) => [agent, new Set(ids)])),
});
const me = { kind: "human" as const, id: "me" };
const ceo = { kind: "agent" as const, id: "ceo" };
const leo = { kind: "human" as const, id: "leo" };
const eng = { kind: "agent" as const, id: "eng" };
const mine = msg(5, "human", "me");

describe("readState", () => {
  it("is Sent until anything moves", () => {
    expect(readState({ message: mine, roomKind: "dm", messages: [mine], receipts: receipts(), members: [me, ceo], viewerId: "me" })).toEqual({ kind: "sent" });
  });

  it("is Delivered once the DM agent's turn was accepted, Read once its run started", () => {
    const base = { message: mine, roomKind: "dm" as const, messages: [mine], members: [me, ceo], viewerId: "me" };
    expect(readState({ ...base, receipts: receipts({ deliveries: [["ceo", 5]] }) })).toEqual({ kind: "delivered" });
    expect(readState({ ...base, receipts: receipts({ deliveries: [["ceo", 5]], agentReads: [["ceo", ["m5"]]] }) })).toEqual({ kind: "read" });
  });

  it("does not call an un-addressed channel message delivered", () => {
    expect(readState({ message: mine, roomKind: "channel", messages: [mine], receipts: receipts({ deliveries: [["ceo", 9]] }), members: [me, ceo, leo], viewerId: "me" }))
      .toEqual({ kind: "sent" });
    const addressed = msg(5, "human", "me", null, null, [{ kind: "agent", id: "ceo" }]);
    expect(readState({ message: addressed, roomKind: "channel", messages: [addressed], receipts: receipts({ deliveries: [["ceo", 9]] }), members: [me, ceo, leo], viewerId: "me" }))
      .toEqual({ kind: "delivered" });
  });

  it("is Delivered from addressedAgentIds alone -- a name-addressed message with no @mention", () => {
    // "Hey ceo, ..." addresses ceo without an @mention at all.
    const nameAddressed = msg(5, "human", "me", null, null, [], ["ceo"]);
    expect(readState({ message: nameAddressed, roomKind: "channel", messages: [nameAddressed], receipts: receipts({ deliveries: [["ceo", 9]] }), members: [me, ceo, leo], viewerId: "me" }))
      .toEqual({ kind: "delivered" });
  });

  it("trusts a present addressedAgentIds over mentions, in both directions", () => {
    // An empty addressedAgentIds says nobody was addressed, even though ceo is @mentioned --
    // e.g. the reply-cap muted line. The old mentions-only guess must not override it.
    const mentionedButNotAddressed = msg(5, "human", "me", null, null, [{ kind: "agent", id: "ceo" }], []);
    expect(readState({ message: mentionedButNotAddressed, roomKind: "channel", messages: [mentionedButNotAddressed], receipts: receipts({ deliveries: [["ceo", 9]] }), members: [me, ceo, leo], viewerId: "me" }))
      .toEqual({ kind: "sent" });
  });

  it("stays Sent for a plain message Jev merely routed as a room_event candidate", () => {
    // ceo was an eligible `active` listener, not addressed -- addressedAgentIds excludes
    // room_event candidates, so even though `_room_dispatch` advanced ceo's delivery
    // cursor past this message (Jev picked someone else, or nobody), it must read Sent,
    // never Delivered, until ceo actually reads or replies.
    const ambient = msg(5, "human", "me", null, null, [], []);
    expect(readState({ message: ambient, roomKind: "channel", messages: [ambient], receipts: receipts({ deliveries: [["ceo", 9]] }), members: [me, ceo, leo], viewerId: "me" }))
      .toEqual({ kind: "sent" });
  });

  it("is Read in a human DM once their cursor passes it", () => {
    expect(readState({ message: mine, roomKind: "dm", messages: [mine], receipts: receipts({ reads: [["leo", 5], ["me", 5]] }), members: [me, leo], viewerId: "me" }))
      .toEqual({ kind: "read" });
  });

  it("counts distinct readers in a group from cursors, runs and replies", () => {
    const messages = [mine, msg(6, "agent", "ceo", "run-1")];
    expect(readState({ message: mine, roomKind: "channel", messages, receipts: receipts({ reads: [["leo", 5], ["me", 6]], agentReads: [["eng", ["m5"]]] }), members: [me, ceo, leo, eng], viewerId: "me" }))
      .toEqual({ kind: "read-by", count: 3 });
  });

  it("ignores an agent post made outside a room turn, my own cursor and ex-members", () => {
    const messages = [mine, msg(6, "agent", "ceo", null), msg(7, "human", "gone")];
    expect(readState({ message: mine, roomKind: "channel", messages, receipts: receipts({ reads: [["me", 9], ["gone", 9]] }), members: [me, ceo, leo], viewerId: "me" }))
      .toEqual({ kind: "sent" });
  });

  it("ignoreHumanCursors: a room-wide human read cursor proves nothing, but a later reply or agent read still does", () => {
    const base = { message: mine, roomKind: "channel" as const, members: [me, ceo, leo], viewerId: "me", ignoreHumanCursors: true };
    // leo's cursor passed this thread reply only because they read a LATER top-level message —
    // never opened the thread. That must not read as Read.
    expect(readState({ ...base, messages: [mine], receipts: receipts({ reads: [["leo", 9]] }) })).toEqual({ kind: "sent" });
    // leo posting a later reply IN the thread is positive evidence.
    const withReply = [mine, msg(6, "human", "leo")];
    expect(readState({ ...base, messages: withReply, receipts: receipts({ reads: [["leo", 9]] }) })).toEqual({ kind: "read-by", count: 1 });
    // An agent read (keyed by messageId, not a room-wide cursor) still counts.
    expect(readState({ ...base, messages: [mine], receipts: receipts({ reads: [["leo", 9]], agentReads: [["ceo", ["m5"]]] }) })).toEqual({ kind: "read-by", count: 1 });
  });
});

describe("readStateLabel", () => {
  it("words each state", () => {
    expect(readStateLabel({ kind: "sent" })).toBe("Sent");
    expect(readStateLabel({ kind: "delivered" })).toBe("Delivered");
    expect(readStateLabel({ kind: "read" })).toBe("Read");
    expect(readStateLabel({ kind: "read-by", count: 2 })).toBe("Read by 2");
  });
});

describe("lastOwnMessageId", () => {
  it("finds my newest live canonical message", () => {
    const list = [msg(1, "human", "me"), msg(2, "agent", "ceo", "r"), msg(3, "human", "me", null, "2026-09-26T00:00:00Z"), msg(0, "human", "me")];
    expect(lastOwnMessageId(list, "me")).toBe("m1");
    expect(lastOwnMessageId([], "me")).toBeNull();
  });
});

describe("unpromptedLabel", () => {
  it("labels an agent message posted outside a room turn, and nothing else", () => {
    expect(unpromptedLabel(msg(1, "agent", "ceo", null))).toBe("on its own");
    expect(unpromptedLabel(msg(1, "agent", "ceo", "run-1"))).toBeNull();
    expect(unpromptedLabel(msg(1, "human", "leo"))).toBeNull();
    expect(unpromptedLabel(msg(1, "agent", "ceo", null, "2026-09-26T00:00:00Z"))).toBeNull();
  });
});
