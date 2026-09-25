import { describe, expect, it } from "vitest";
import { canSendToSession, markSessionsSeen, sendBlockedReason, sessionLabel, sessionMatches, sessionStatus, sessionTitle, sessionToolName, sessionUnread, workspaceSections } from "./localSessionLabels";
import type { LocalSession } from "./types";

const base = { capabilities: { sendMode: "queue" }, controlUnavailableReason: null, capabilitiesVerified: true } as unknown as LocalSession;

describe("session labels", () => {
  it("splits the tool name from the title, falling back to the raw id for an unknown tool", () => {
    const s = { ...base, provider: "claude", title: " Fix relay ", sessionId: "abcdef123" } as LocalSession;
    expect(sessionTitle(s)).toBe("Fix relay");
    expect(sessionTitle({ ...s, title: null })).toBe("abcdef12");
    expect(sessionLabel(s)).toBe("Claude · Fix relay");
    expect(sessionToolName("codex")).toBe("Codex");
    expect(sessionToolName("gemini")).toBe("gemini");
  });

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

describe("sendBlockedReason", () => {
  it("names the specific reason, and null when sending works", () => {
    const s = { ...base, status: "idle" } as LocalSession;
    expect(sendBlockedReason(s)).toBeNull();
    expect(sendBlockedReason({ ...s, status: "offline" })).toMatch(/^Offline/);
    expect(sendBlockedReason({ ...s, controlUnavailableReason: "history-gap" })).toMatch(/^History has a gap/);
    expect(sendBlockedReason({ ...s, capabilities: { sendMode: "none" } } as unknown as LocalSession)).toBe("This session does not accept remote input.");
  });
});

describe("session unread and filter", () => {
  const s = (sessionId: string, latestEventSeq: number, status: LocalSession["status"] = "busy") =>
    ({ ...base, sessionId, latestEventSeq, status, provider: "claude", title: "Fix login", cwdName: "web" }) as LocalSession;

  it("baselines first sight, advances only the open session, and reads unread vs finished", () => {
    let seen = markSessionsSeen({}, [s("a", 3), s("b", 5)], null);
    expect(seen).toEqual({ a: 3, b: 5 });
    expect(markSessionsSeen(seen, [s("a", 3)], null)).toBe(seen); // nothing moved: same object
    expect(sessionUnread(s("a", 3), seen)).toBeNull();
    expect(sessionUnread(s("a", 4), seen)).toBe("unread");
    expect(sessionUnread(s("a", 4, "idle"), seen)).toBe("finished");
    expect(sessionUnread(s("a", 4, "ended"), seen)).toBe("finished");
    expect(sessionUnread(s("new", 9), seen)).toBeNull(); // never seen: no baseline yet
    seen = markSessionsSeen(seen, [s("a", 8), s("b", 9)], "a");
    expect(seen).toEqual({ a: 8, b: 5 });
  });

  it("filters by title, folder, tool or owner", () => {
    expect(sessionMatches(s("a", 1), "LOGIN")).toBe(true);
    expect(sessionMatches(s("a", 1), "web")).toBe(true);
    expect(sessionMatches(s("a", 1), "claude")).toBe(true);
    expect(sessionMatches(s("a", 1), "ann", "Ann")).toBe(true);
    expect(sessionMatches(s("a", 1), "codex")).toBe(false);
    expect(sessionMatches(s("a", 1), "  ")).toBe(true);
  });
});
