import { describe, expect, it } from "vitest";

import { auditLine, canControlSession, sessionFacts, usageLine, withAuditNotices } from "./sessionControls";
import type { LocalSession, LocalSessionAuditEntry, LocalSessionEvent } from "./types";

const names: Record<string, string> = { user_aden: "Aden", user_owner: "Prasiddha" };
const nameOf = (id: string) => names[id] ?? "Teammate";

const session = (changes: Partial<LocalSession> = {}) => ({ status: "busy", teamControl: false, ...changes }) as LocalSession;

describe("canControlSession", () => {
  it("is the owner's by default and a teammate's only while the owner opted in", () => {
    expect(canControlSession(session(), true)).toBe(true);
    expect(canControlSession(session(), false)).toBe(false);
    expect(canControlSession(session({ teamControl: true }), false)).toBe(true);
    expect(canControlSession(session({ teamControl: true, status: "ended" }), false)).toBe(false);
  });

  it("lets in exactly the people the owner allowed", () => {
    const s = session({ allowedSenders: ["user_aden"] });
    expect(canControlSession(s, false, "user_aden")).toBe(true);
    expect(canControlSession(s, false, "user_other")).toBe(false);
    expect(canControlSession(s, false)).toBe(false);
    expect(canControlSession({ ...s, status: "ended" }, false, "user_aden")).toBe(false);
  });
});

describe("auditLine for asking to join", () => {
  it("names the person a decision was about", () => {
    expect(auditLine({ at: "", action: "join_requested", actorUserId: "user_aden" }, nameOf)).toBe("Aden asked to join this session");
    expect(auditLine({ at: "", action: "join_allowed", actorUserId: "user_owner", subjectUserId: "user_aden" }, nameOf))
      .toBe("Prasiddha let Aden send to this session");
    expect(auditLine({ at: "", action: "join_declined", actorUserId: "user_owner", subjectUserId: "user_aden" }, nameOf))
      .toBe("Prasiddha declined Aden's request to join");
    expect(auditLine({ at: "", action: "sender_removed", actorUserId: "user_owner", subjectUserId: "user_aden" }, nameOf))
      .toBe("Prasiddha removed Aden from this session");
  });
});

describe("auditLine", () => {
  it("names who did what, from Lightbulb", () => {
    expect(auditLine({ at: "", action: "approve", actorUserId: "user_aden" }, nameOf)).toBe("Aden approved a permission prompt from Lightbulb");
    expect(auditLine({ at: "", action: "deny", actorUserId: "user_aden", state: "rejected" }, nameOf))
      .toBe("Aden denied a permission prompt from Lightbulb (not delivered)");
    expect(auditLine({ at: "", action: "team_control_on", actorUserId: "user_owner" }, nameOf)).toBe("Prasiddha let teammates control this session");
    expect(auditLine({ at: "", action: "team_control_ended" }, nameOf)).toBe("Teammate control turned off when the session ended");
  });
});

describe("withAuditNotices", () => {
  const event = (seq: number, ts?: string): LocalSessionEvent => ({ seq, kind: "text", role: "assistant", text: `e${seq}`, truncated: false, ...(ts ? { ts } : {}) });
  const entry = (at: string, action: LocalSessionAuditEntry["action"] = "approve"): LocalSessionAuditEntry => ({ at, action, actorUserId: "user_aden" });

  it("weaves each entry after the last event before it, as a system notice", () => {
    const events = [event(1, "2026-09-25T10:00:00Z"), event(2), event(3, "2026-09-25T10:05:00Z")];
    const out = withAuditNotices(events, [entry("2026-09-25T10:06:00Z"), entry("2026-09-25T10:01:00Z", "team_control_on")], nameOf);
    expect(out.map(e => e.text)).toEqual(["e1", "e2", "Aden let teammates control this session", "e3", "Aden approved a permission prompt from Lightbulb"]);
    const notice = out[2];
    expect(notice.kind).toBe("text");
    expect(notice.role).toBe("system");
    expect(new Set(out.map(e => e.seq)).size).toBe(out.length);
  });

  it("leaves follow-ups (the prompt shows its sender) and entries older than the loaded window out", () => {
    const events = [event(10, "2026-09-25T10:00:00Z")];
    const out = withAuditNotices(events, [entry("2026-09-25T09:00:00Z"), entry("2026-09-25T10:01:00Z", "send")], nameOf);
    expect(out).toEqual(events);
  });
});

describe("sessionFacts", () => {
  it("reads the harness's own model and mode, and says nothing it was not told", () => {
    expect(sessionFacts({ model: "claude-opus-5-5", permissionMode: "acceptEdits" })).toBe("claude-opus-5-5 · Auto-accept edits");
    expect(sessionFacts({ permissionMode: "plan" })).toBe("Plan mode");
    expect(sessionFacts({ permissionMode: "future-mode" })).toBe("future-mode");
    expect(sessionFacts({})).toBeNull();
  });
});

describe("usageLine", () => {
  it("compacts the harness's own counts and never invents a cost", () => {
    expect(usageLine({ inputTokens: 1200, outputTokens: 340, cacheReadTokens: 90000, cacheWriteTokens: 800 })).toBe("1.2k in · 340 out · 90.8k cached");
    expect(usageLine({ inputTokens: 3, outputTokens: 2_500_000, cacheReadTokens: 0, cacheWriteTokens: 0 })).toBe("3 in · 2.5M out");
    expect(usageLine(undefined)).toBeNull();
  });
});
