import { describe, expect, it } from "vitest";

import { parseLiveEvent } from "../live/types";
import { applyRoomActivity, pruneRoomActivity, roomLiveLine, type RoomActivity } from "./roomActivity";

const R = "22222222-2222-4222-8222-222222222222";
const empty: RoomActivity = new Map();
const ann = { kind: "human" as const, id: "u1", displayName: "Ann" };
const ceo = { kind: "agent" as const, id: "ceo", displayName: "CEO" };
const soon = (ms: number) => new Date(Date.parse("2026-09-26T12:00:00Z") + ms).toISOString();
const NOW = Date.parse("2026-09-26T12:00:00Z");

describe("room activity", () => {
  it("parses the wire event", () => {
    const event = parseLiveEvent({
      schemaVersion: 1, eventId: "e", type: "room.activity.changed", occurredAt: soon(0), roomId: R,
      payload: { roomId: R, actor: ann, state: "typing", expiresAt: soon(3000) },
    });
    expect(event?.type).toBe("room.activity.changed");
    expect(parseLiveEvent({ schemaVersion: 1, eventId: "e", type: "room.activity.changed", occurredAt: soon(0), payload: { roomId: R, actor: ann, state: "dancing" } })).toBeNull();
  });

  it("says who is typing, and typing wins over working", () => {
    let s = applyRoomActivity(empty, { roomId: R, actor: ceo, state: "working" });
    expect(roomLiveLine(s, R, "channel", NOW)).toBe("CEO is working…");
    expect(roomLiveLine(s, R, "dm", NOW)).toBe("working…");
    s = applyRoomActivity(s, { roomId: R, actor: ann, state: "typing", expiresAt: soon(3000) });
    expect(roomLiveLine(s, R, "channel", NOW)).toBe("Ann is typing…");
  });

  it("clears on idle and on expiry", () => {
    let s = applyRoomActivity(empty, { roomId: R, actor: ann, state: "typing", expiresAt: soon(3000) });
    expect(roomLiveLine(pruneRoomActivity(s, NOW + 4000), R, "dm", NOW + 4000)).toBeNull();
    s = applyRoomActivity(s, { roomId: R, actor: ann, state: "idle" });
    expect(roomLiveLine(s, R, "dm", NOW)).toBeNull();
  });

  it("uses the web's overflow wording", () => {
    let s: RoomActivity = empty;
    for (const [i, n] of ["Ann", "Bo", "Cy", "Di"].entries()) {
      s = applyRoomActivity(s, { roomId: R, actor: { kind: "human", id: `u${i}`, displayName: n }, state: "typing", expiresAt: soon(3000) });
    }
    expect(roomLiveLine(s, R, "channel", NOW)).toBe("Ann, Bo, Cy and others are typing…");
  });

  // Controller ruling (2026-09-26): "working" hints now carry an expiry (now + 90s), refreshed
  // by every run heartbeat, so honour expiresAt for BOTH typing and working. Replaces the
  // brief's "working and idle carry no expiry" test.
  it("a working entry disappears after its expiresAt", () => {
    const s = applyRoomActivity(empty, { roomId: R, actor: ceo, state: "working", expiresAt: soon(90_000) });
    expect(roomLiveLine(pruneRoomActivity(s, NOW + 89_000), R, "dm", NOW + 89_000)).toBe("working…");
    expect(roomLiveLine(pruneRoomActivity(s, NOW + 90_001), R, "dm", NOW + 90_001)).toBeNull();
  });

  it("a later working event refreshes the expiry", () => {
    let s = applyRoomActivity(empty, { roomId: R, actor: ceo, state: "working", expiresAt: soon(90_000) });
    s = applyRoomActivity(s, { roomId: R, actor: ceo, state: "working", expiresAt: soon(180_000) });
    expect(roomLiveLine(pruneRoomActivity(s, NOW + 90_001), R, "dm", NOW + 90_001)).toBe("working…");
  });

  it("idle clears working immediately, ahead of its expiresAt", () => {
    let s = applyRoomActivity(empty, { roomId: R, actor: ceo, state: "working", expiresAt: soon(90_000) });
    s = applyRoomActivity(s, { roomId: R, actor: ceo, state: "idle" });
    expect(roomLiveLine(s, R, "dm", NOW)).toBeNull();
  });

  it("typing still expires", () => {
    const s = applyRoomActivity(empty, { roomId: R, actor: ann, state: "typing", expiresAt: soon(3000) });
    expect(roomLiveLine(pruneRoomActivity(s, NOW + 4000), R, "dm", NOW + 4000)).toBeNull();
  });
});
