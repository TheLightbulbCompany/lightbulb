import type { LocalSession } from "@lightbulb/shared";

import { workspaceSections } from "../localSessions";

const s = (over: Partial<LocalSession>): LocalSession => ({ sessionId: "x", deviceId: "d", humanUserId: "h", provider: "claude", providerVersion: "2", generation: "g", status: "idle", activeTurnId: null,
  capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true, controlUnavailableReason: null, latestEventSeq: 0, ...over });

it("groups workspace sessions by person, mine first, busy first, then most recent", () => {
  const sections = workspaceSections([
    s({ sessionId: "b1", humanUserId: "bob" }),
    s({ sessionId: "a-idle", humanUserId: "alice", lastEventAt: "2026-09-14T10:00:00Z" }),
    s({ sessionId: "a-busy", humanUserId: "alice", status: "busy", lastEventAt: "2026-09-14T09:00:00Z" }),
    s({ sessionId: "mine", humanUserId: "me" }),
  ], "me", (id) => ({ alice: "Alice", bob: "Bob" }[id] ?? "Room member"));
  expect(sections.map((section) => section.title)).toEqual(["You", "Alice", "Bob"]);
  expect(sections[1].data.map((row) => row.sessionId)).toEqual(["a-busy", "a-idle"]);
});

it("breaks a tie between same-status sessions by most recent lastEventAt", () => {
  const sections = workspaceSections([
    s({ sessionId: "older", humanUserId: "alice", status: "idle", lastEventAt: "2026-09-14T09:00:00Z" }),
    s({ sessionId: "newer", humanUserId: "alice", status: "idle", lastEventAt: "2026-09-14T10:00:00Z" }),
  ], "me", (id) => ({ alice: "Alice" }[id] ?? "Room member"));
  expect(sections[0].data.map((row) => row.sessionId)).toEqual(["newer", "older"]);
});
