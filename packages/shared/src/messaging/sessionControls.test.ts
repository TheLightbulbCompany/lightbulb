// @vitest-environment jsdom
import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/error";

import { auditLine, canControlSession, useSessionApproval, useShownInputMatches, sessionFacts, usageLine, withAuditNotices } from "./sessionControls";
import type { LocalSession, LocalSessionAuditEntry, LocalSessionCommandReceipt, LocalSessionEvent } from "./types";

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

describe("auditLine for the session link", () => {
  it("names who took a copy, from the entry's own detail", () => {
    const entry = { at: "2026-10-05T10:00:00Z", action: "session_copied", actorUserId: "user_out", detail: "Sam Lee" } as const;
    expect(auditLine(entry, () => "A teammate")).toBe("Sam Lee took a copy to continue in their Claude Code");
    expect(auditLine({ ...entry, action: "share_link_audience", actorUserId: "alice", detail: "anyone" }, () => "You"))
      .toBe("You changed who the link is for");
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

  it("says you, not You, when the subject is the viewer", () => {
    const viewer = (id: string) => (id === "user_aden" ? "You" : nameOf(id));
    expect(auditLine({ at: "", action: "join_allowed", actorUserId: "user_owner", subjectUserId: "user_aden" }, viewer))
      .toBe("Prasiddha let you send to this session");
    expect(auditLine({ at: "", action: "join_declined", actorUserId: "user_owner", subjectUserId: "user_aden" }, viewer))
      .toBe("Prasiddha declined your request to join");
  });
});

describe("auditLine for asking to pick up", () => {
  it("names the person asking and the one declined", () => {
    expect(auditLine({ at: "", action: "pickup_requested", actorUserId: "user_aden" }, nameOf)).toBe("Aden asked to pick up this session");
    expect(auditLine({ at: "", action: "pickup_request_declined", actorUserId: "user_owner", subjectUserId: "user_aden" }, nameOf))
      .toBe("Prasiddha declined Aden's request to pick up this session");
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

describe("useSessionApproval", () => {
  const pending = { approvalId: "a1", tool: "Bash", summary: "ls", inputDigest: "abc", exact: false, full: true, modes: [], input: { command: "ls" } };
  const mount = (p: object) => {
    const submit = vi.fn<(id: string, command: object) => Promise<LocalSessionCommandReceipt>>(async () => ({ state: "dispatched" }) as LocalSessionCommandReceipt);
    const sha = vi.fn(async () => "abc");
    const row = session({ sessionId: "s1", generation: "g1", pendingApproval: p } as Partial<LocalSession>);
    const { result } = renderHook(() => useSessionApproval({ submitLocalSessionCommand: submit }, row, () => "r1", sha));
    return { result, submit, sha };
  };

  it("a full approval sends the digest of what was shown; a deny does not", async () => {
    const { result, submit, sha } = mount(pending);
    await act(() => result.current.answer({ decision: "allow" }));
    expect(sha).toHaveBeenCalledWith('{"command":"ls"}');
    expect(submit).toHaveBeenLastCalledWith("s1", expect.objectContaining({ kind: "approve", decision: "allow", shownDigest: "abc" }));
    await act(() => result.current.answer({ decision: "deny" }));
    expect(submit.mock.calls[1][1]).not.toHaveProperty("shownDigest");
  });

  const conflict = () => new ApiError(409, { code: "LOCAL_SESSION_CONFLICT" }, "Session changed. Try again.");
  const live = (p?: object) => session({ sessionId: "s1", generation: "g1", status: p ? "waiting-approval" : "busy", pendingApproval: p } as Partial<LocalSession>);
  const mountWith = (getLocalSession: () => Promise<LocalSession>) => {
    const submit = vi.fn<(id: string, command: object) => Promise<LocalSessionCommandReceipt>>(async () => { throw conflict(); });
    const hook = renderHook(({ s }) => useSessionApproval({ submitLocalSessionCommand: submit, getLocalSession: vi.fn(getLocalSession) }, s, () => "r1", async () => "abc"),
      { initialProps: { s: live(pending) } });
    return { ...hook, submit };
  };

  it("a conflict while the prompt is still open keeps the card, with a Try again note", async () => {
    const { result } = mountWith(async () => live(pending));
    await act(() => result.current.answer({ decision: "deny" }));
    expect(result.current.pending).toEqual(pending);
    expect(result.current.state?.error).toBe("Could not send the answer. Try again.");
  });

  it("a conflict after the prompt closed hides the card; the same prompt on a later row shows again", async () => {
    const { result, rerender } = mountWith(async () => live());
    await act(() => result.current.answer({ decision: "deny" }));
    expect(result.current.pending).toBeUndefined();
    rerender({ s: live(pending) });
    expect(result.current.pending).toEqual(pending);
  });

  it("A's rejected receipt arriving after the row moved to prompt B leaves B's card up", async () => {
    let settle: (r: LocalSessionCommandReceipt) => void = () => {};
    const submit = vi.fn(() => new Promise<LocalSessionCommandReceipt>(resolve => { settle = resolve; }));
    const b = { ...pending, approvalId: "b1" };
    const { result, rerender } = renderHook(({ s }) => useSessionApproval({ submitLocalSessionCommand: submit }, s, () => "r1", async () => "abc"),
      { initialProps: { s: live(pending) } });
    let answered: Promise<void> = Promise.resolve();
    act(() => { answered = result.current.answer({ decision: "deny" }); });
    rerender({ s: live(b) });
    await act(async () => { settle({ state: "rejected" } as LocalSessionCommandReceipt); await answered; });
    expect(result.current.pending).toEqual(b);
  });

  it("an unsupported answer keeps the card with the server's reason", async () => {
    const refused = mount(pending);
    refused.submit.mockRejectedValueOnce(new ApiError(409, { code: "LOCAL_SESSION_UNSUPPORTED" }, "This prompt can only be approved in the terminal."));
    await act(() => refused.result.current.answer({ decision: "allow" }));
    expect(refused.result.current.pending).toEqual(pending);
    expect(refused.result.current.state?.error).toBe("This prompt can only be approved in the terminal.");
  });

  it("an answer the Mac rejects (answered in the terminal first) removes the card", async () => {
    const { result, submit } = mount(pending);
    submit.mockResolvedValueOnce({ state: "rejected" } as LocalSessionCommandReceipt);
    await act(() => result.current.answer({ decision: "deny" }));
    expect(result.current.pending).toBeUndefined();
  });

  it("an approval from an older Mac (no full) sends no digest", async () => {
    const { result, submit, sha } = mount({ ...pending, full: undefined, input: undefined, exact: true });
    await act(() => result.current.answer({ decision: "allow" }));
    expect(sha).not.toHaveBeenCalled();
    expect(submit.mock.calls[0][1]).not.toHaveProperty("shownDigest");
  });
});

describe("useShownInputMatches", () => {
  const pending = { approvalId: "a1", tool: "Bash", summary: "ls", inputDigest: "abc", exact: false, full: true, modes: [], input: { command: "ls" } };
  it("is false until the hash of what is shown is known, then true only on a match", async () => {
    let resolve: (hex: string) => void = () => undefined;
    const sha = vi.fn(() => new Promise<string>(r => { resolve = r; }));
    const { result } = renderHook(() => useShownInputMatches(pending, sha));
    expect(result.current).toBe(false);
    await act(async () => resolve("abc"));
    expect(result.current).toBe(true);
  });
  it("a new input under the same prompt reads false until it is hashed", async () => {
    let resolve: (hex: string) => void = () => undefined;
    const sha = vi.fn(() => new Promise<string>(r => { resolve = r; }));
    const { result, rerender } = renderHook(({ p }) => useShownInputMatches(p, sha), { initialProps: { p: pending } });
    await act(async () => resolve("abc"));
    expect(result.current).toBe(true);
    rerender({ p: { ...pending, input: { command: "rm -rf /" } } });
    expect(result.current).toBe(false);
  });
  it("is false on a mismatch, a hash failure, or a prompt that is not full", async () => {
    const other = vi.fn(async () => "def");
    const mismatch = renderHook(() => useShownInputMatches(pending, other));
    const failed = renderHook(() => useShownInputMatches(pending, async () => { throw new Error("no subtle"); }));
    const partial = renderHook(() => useShownInputMatches({ ...pending, full: false }, async () => "abc"));
    await waitFor(() => expect(other).toHaveBeenCalled());
    await act(async () => undefined);
    expect(mismatch.result.current).toBe(false);
    expect(failed.result.current).toBe(false);
    expect(partial.result.current).toBe(false);
    const hidden = renderHook(() => useShownInputMatches({ ...pending, input: { command: "ls \u202E" } }, async () => "abc"));
    await act(async () => undefined);
    expect(hidden.result.current).toBe(false);
    const nbsp = renderHook(() => useShownInputMatches({ ...pending, input: { command: "rm\u00A0-rf x" } }, async () => "abc"));
    await act(async () => undefined);
    expect(nbsp.result.current).toBe(false);
  });
});
