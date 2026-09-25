import { describe, expect, it } from "vitest";

import { auditLine, canControlSession, withAuditNotices } from "./sessionControls";
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
