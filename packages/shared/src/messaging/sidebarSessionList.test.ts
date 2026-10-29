import { describe, expect, it } from "vitest";
import { markSessionRead, markSessionUnread, sessionUnread, sidebarSessionList, type SessionMarks } from "./localSessionLabels";
import { mergeChats } from "./chatList";
import type { LocalSession } from "./types";

const NOW = Date.parse("2026-10-27T12:00:00Z");
const s = (sessionId: string, status: LocalSession["status"], lastEventAt: string, extra: Partial<LocalSession> = {}) =>
  ({ sessionId, status, lastEventAt, latestEventSeq: 5, ...extra }) as LocalSession;
const none: SessionMarks = { pinned: [], archived: {} };
const ids = (entries: { session: LocalSession }[]) => entries.map((e) => e.session.sessionId);

describe("sidebarSessionList", () => {
  const entries = [
    s("live-old", "idle", "2026-10-27T08:00:00Z"),
    s("live-new", "busy", "2026-10-27T11:00:00Z"),
    s("ended-old", "ended", "2026-10-25T10:00:00Z"),
    s("ended-new", "ended", "2026-10-27T11:30:00Z"),
  ].map((session) => ({ session }));

  it("keeps ended sessions: live first in the order given, then the rest newest first", () => {
    expect(ids(sidebarSessionList(entries, none, NOW).list)).toEqual(["live-old", "live-new", "ended-new", "ended-old"]);
  });

  it("lists a session ended on purpose like any ended one, and never one offline for days", () => {
    const { list, archived } = sidebarSessionList([
      ...entries,
      { session: s("on-purpose", "ended", "2026-10-27T11:59:00Z", { endedOnPurpose: true }) },
      { session: s("lost", "offline", "2026-10-20T11:59:00Z") },
    ], none, NOW);
    expect(ids(list)).toContain("on-purpose");
    expect(ids(list)).not.toContain("lost");
    expect(ids(archived)).toEqual([]);
  });

  it("puts pinned rows on top, ended ones included", () => {
    expect(ids(sidebarSessionList(entries, { pinned: ["ended-old"], archived: {} }, NOW).list)[0]).toBe("ended-old");
  });

  it("moves an archived row to Archived until it has new output", () => {
    const marks = { pinned: [], archived: { "ended-new": 5 } };
    const before = sidebarSessionList(entries, marks, NOW);
    expect(ids(before.archived)).toEqual(["ended-new"]);
    expect(ids(before.list)).not.toContain("ended-new");
    const moved = entries.map((e) => e.session.sessionId === "ended-new" ? { session: { ...e.session, latestEventSeq: 6 } } : e);
    expect(ids(sidebarSessionList(moved, marks, NOW).list)).toContain("ended-new");
  });
});

describe("mark unread / read", () => {
  it("lowers and restores the seen baseline", () => {
    const idle = s("a", "idle", "2026-10-27T11:00:00Z");
    const unread = markSessionUnread({ a: 5 }, idle);
    expect(sessionUnread(idle, unread, NOW)).toBe("finished");
    expect(sessionUnread(idle, markSessionRead(unread, idle), NOW)).toBeNull();
    const fresh = s("b", "busy", "2026-10-27T11:00:00Z", { latestEventSeq: 0 });
    expect(sessionUnread(fresh, markSessionUnread({}, fresh), NOW)).toBe("unread");
  });
});

describe("mergeChats", () => {
  it("keeps a session ended on purpose, as any ended one (desktop #4453)", () => {
    const chats = mergeChats([], [s("x", "ended", "2026-10-27T11:00:00Z", { endedOnPurpose: true }), s("y", "ended", "2026-10-27T11:00:00Z")], { now: NOW });
    expect(chats.map((c) => c.key).sort()).toEqual(["session:x", "session:y"]);
  });
});
