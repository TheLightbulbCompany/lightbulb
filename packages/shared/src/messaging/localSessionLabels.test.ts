import { describe, expect, it } from "vitest";
import { canSendToSession, sessionStatus, workspaceSections } from "./localSessionLabels";
import type { LocalSession } from "./types";

const base = { capabilities: { sendMode: "queue" }, controlUnavailableReason: null, capabilitiesVerified: true } as unknown as LocalSession;

describe("session labels", () => {
  it("labels ended sessions and refuses to send to them", () => {
    expect(sessionStatus.ended).toBe("Ended");
    expect(canSendToSession({ ...base, status: "ended" })).toBe(false);
    expect(canSendToSession({ ...base, status: "idle" })).toBe(true);
  });

  it("labels waiting-approval as input needed, covering both permission prompts and questions", () => {
    expect(sessionStatus["waiting-approval"]).toBe("Waiting for input");
  });
});

describe("workspaceSections", () => {
  const s = (over: Partial<LocalSession>): LocalSession => ({
    sessionId: "x", deviceId: "d", humanUserId: "h", provider: "claude", providerVersion: "2", generation: "g", status: "idle", activeTurnId: null,
    capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true, controlUnavailableReason: null, latestEventSeq: 0, ...over,
  });
  const name = (id: string) => ({ alice: "Alice", bob: "Bob" }[id] ?? "Room member");

  it("groups mine first, then people by name, each sorted busy before waiting before idle, then most recent", () => {
    const sections = workspaceSections([
      s({ sessionId: "b-idle", humanUserId: "bob", lastEventAt: "2026-09-14T10:00:00Z" }),
      s({ sessionId: "a-idle-old", humanUserId: "alice", lastEventAt: "2026-09-14T09:00:00Z" }),
      s({ sessionId: "a-idle-new", humanUserId: "alice", lastEventAt: "2026-09-14T11:00:00Z" }),
      s({ sessionId: "a-busy", humanUserId: "alice", status: "busy", lastEventAt: "2026-09-14T08:00:00Z" }),
      s({ sessionId: "a-wait", humanUserId: "alice", status: "waiting-approval" }),
      s({ sessionId: "me", humanUserId: "me", lastEventAt: "2026-09-14T01:00:00Z" }),
    ], "me", name);
    expect(sections.map((section) => section.title)).toEqual(["You", "Alice", "Bob"]);
    expect(sections.flatMap((section) => section.data.map((row) => row.sessionId)))
      .toEqual(["me", "a-busy", "a-wait", "a-idle-new", "a-idle-old", "b-idle"]);
  });

  it("breaks a tie between same-status sessions by most recent lastEventAt", () => {
    const sections = workspaceSections([
      s({ sessionId: "older", humanUserId: "alice", status: "idle", lastEventAt: "2026-09-14T09:00:00Z" }),
      s({ sessionId: "newer", humanUserId: "alice", status: "idle", lastEventAt: "2026-09-14T10:00:00Z" }),
    ], "me", name);
    expect(sections[0].data.map((row) => row.sessionId)).toEqual(["newer", "older"]);
  });
});
