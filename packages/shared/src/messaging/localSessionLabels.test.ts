import { describe, expect, it } from "vitest";
import { changesLabel, sessionPrLabel, sessionSignalsLine, canSendToSession, endedWhy, markSessionsSeen, sendBlockedReason, SESSION_ARCHIVE_AFTER_MS, sessionArchived, sessionHistory, sessionLabel, sessionLive, sessionMatches, sessionState, sessionStateAction, sessionStateLabel, sessionStatus, sessionTitle, sessionWaitingLine, sessionsNeedingYou, SESSION_NOTIFICATION_DEFAULTS, sessionToolName, sessionAgentName, SESSION_AGENTS, sessionUnread, workspaceSections } from "./localSessionLabels";
import type { LocalSession } from "./types";

const base = { capabilities: { sendMode: "queue" }, controlUnavailableReason: null, capabilitiesVerified: true } as unknown as LocalSession;

describe("endedWhy", () => {
  it("names a login-session restart and nothing else", () => {
    expect(endedWhy({ status: "ended", endedReason: "login-session-ended" }, "your Mac")).toBe("your Mac's login session restarted");
    expect(endedWhy({ status: "ended", endedReason: "login-session-ended" }, "Leo’s Mac", "’")).toBe("Leo’s Mac’s login session restarted");
    expect(endedWhy({ status: "ended", endedReason: "exited" }, "your Mac")).toBeNull();
    expect(endedWhy({ status: "idle", endedReason: "login-session-ended" }, "your Mac")).toBeNull();
  });

  it("names a session the Mac stopped while idle, on an ended row only", () => {
    expect(endedWhy({ status: "ended", endedReason: "idle-stopped" }, "your Mac")).toBe("stopped while idle");
    expect(endedWhy({ status: "ended", endedReason: "idle-stopped" }, "Leo’s Mac", "’")).toBe("stopped while idle");
    expect(endedWhy({ status: "idle", endedReason: "idle-stopped" }, "your Mac")).toBeNull();
  });
});

describe("session labels", () => {
  it("splits the tool name from the title, falling back to the raw id for an unknown tool", () => {
    const s = { ...base, provider: "claude", title: " Fix relay ", sessionId: "abcdef123" } as LocalSession;
    expect(sessionTitle(s)).toBe("Fix relay");
    expect(sessionTitle({ ...s, title: null, cwdName: "lightbulb" } as LocalSession)).toBe("New chat");
    expect(sessionTitle({ ...s, title: null, cwdName: null } as LocalSession)).toBe("New chat");
    expect(sessionLabel(s)).toBe("Claude · Fix relay");
    expect(sessionToolName("codex")).toBe("Codex");
    expect(sessionToolName("gemini")).toBe("gemini");
  });

  it("names every agent by its product, never 'Codex' for a non-Claude one", () => {
    expect(["claude", "codex", "cursor", "grok"].map(sessionAgentName)).toEqual(["Claude Code", "Codex", "Cursor", "Grok CLI"]);
    expect(["cursor", "grok"].map(sessionToolName)).toEqual(["Cursor", "Grok"]);
    expect(sessionAgentName("gemini")).toBe("gemini");
    expect(SESSION_AGENTS).toBe("Claude Code, Codex, Cursor and Grok CLI");
  });

  it("shows a title that opens with a URL as its host and the words after it", () => {
    const s = { ...base, provider: "claude", sessionId: "abcdef123" } as LocalSession;
    expect(sessionTitle({ ...s, title: "https://code.claude.com/docs/en/agents?x=1#top we have a problem" })).toBe("code.claude.com · we have a problem");
    expect(sessionTitle({ ...s, title: "http://user:secret@example.com/path" })).toBe("example.com");
    expect(sessionTitle({ ...s, title: "see https://example.com/a" })).toBe("see https://example.com/a"); // only a leading URL
    expect(sessionTitle({ ...s, title: "https://" })).toBe("https://");
    // A name the owner typed is shown as typed.
    expect(sessionTitle({ ...s, title: "x", displayTitle: "https://example.com/board" })).toBe("https://example.com/board");
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

  it("groups mine first, then people by name, each live first and newest activity first, never by status", () => {
    const sections = workspaceSections([
      s({ sessionId: "b-idle", humanUserId: "bob", lastEventAt: "2026-09-14T10:00:00Z" }),
      s({ sessionId: "a-idle-old", humanUserId: "alice", lastEventAt: "2026-09-14T09:00:00Z" }),
      s({ sessionId: "a-idle-new", humanUserId: "alice", lastEventAt: "2026-09-14T11:00:00Z" }),
      // A stale "busy" from hours ago does not jump the queue (owner report 2026-10-13).
      s({ sessionId: "a-busy", humanUserId: "alice", status: "busy", lastEventAt: "2026-09-14T08:00:00Z" }),
      s({ sessionId: "a-ended", humanUserId: "alice", status: "ended", lastEventAt: "2026-09-14T12:00:00Z" }),
      s({ sessionId: "me", humanUserId: "me", lastEventAt: "2026-09-14T01:00:00Z" }),
    ], "me", name);
    expect(sections.map((section) => section.title)).toEqual(["You", "Alice", "Bob"]);
    expect(sections.flatMap((section) => section.data.map((row) => row.sessionId)))
      .toEqual(["me", "a-idle-new", "a-idle-old", "a-busy", "a-ended", "b-idle"]);
  });

  it("falls back to the id for sessions titled alike", () => {
    const sections = workspaceSections([
      s({ sessionId: "b", title: "Fix", humanUserId: "alice" }),
      s({ sessionId: "a", title: "Fix", humanUserId: "alice" }),
    ], "me", name);
    expect(sections[0].data.map((row) => row.sessionId)).toEqual(["a", "b"]);
  });
});

describe("sendBlockedReason", () => {
  it("names the specific reason, and null when sending works", () => {
    const s = { ...base, status: "idle" } as LocalSession;
    expect(sendBlockedReason(s)).toBeNull();
    expect(sendBlockedReason({ ...s, status: "offline" })).toBeNull();
    expect(sendBlockedReason({ ...s, status: "offline", capabilities: { sendMode: "immediate" } } as LocalSession)).toMatch(/cannot queue/);
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

describe("session archive", () => {
  const now = Date.parse("2026-09-25T12:00:00Z");
  const at = (msAgo: number) => new Date(now - msAgo).toISOString();
  const s = (status: LocalSession["status"], lastEventAt: string | null, latestEventSeq = 1) =>
    ({ ...base, sessionId: "a", status, lastEventAt, latestEventSeq }) as LocalSession;

  it("archives an offline session at exactly 72 h, not a millisecond before", () => {
    expect(SESSION_ARCHIVE_AFTER_MS).toBe(72 * 60 * 60 * 1000);
    expect(sessionArchived(s("offline", at(SESSION_ARCHIVE_AFTER_MS - 1)), now)).toBe(false);
    expect(sessionArchived(s("offline", at(SESSION_ARCHIVE_AFTER_MS)), now)).toBe(true);
    expect(sessionArchived(s("offline", null), now)).toBe(false); // no timestamp: never guessed old
  });

  it("leaves the archive by itself when the session comes back online", () => {
    const old = at(SESSION_ARCHIVE_AFTER_MS * 2);
    expect(sessionArchived(s("idle", old), now)).toBe(false);
    expect(sessionArchived(s("busy", old), now)).toBe(false);
  });

  it("lists only mirrored sessions as live", () => {
    const old = at(SESSION_ARCHIVE_AFTER_MS);
    expect(["busy", "waiting-approval", "idle", "unknown"].map((st) => sessionLive(s(st as LocalSession["status"], old)))).toEqual([true, true, true, true]);
    expect(sessionLive(s("offline", at(1000)))).toBe(false);
    expect(sessionLive(s("ended", at(1000)))).toBe(false);
  });

  it("buckets All sessions by local day, newest first", () => {
    const noon = new Date(2026, 8, 25, 12).getTime(); // local noon, so the buckets never depend on the runner's zone
    const h = (hours: number, id: string, status: LocalSession["status"] = "offline") =>
      ({ ...base, sessionId: id, status, lastEventAt: new Date(noon - hours * 3600_000).toISOString() }) as LocalSession;
    const groups = sessionHistory([h(30, "y"), h(1, "t2"), h(0.5, "t1", "busy"), h(24 * 4, "w"), h(24 * 9, "e", "ended"), { ...h(0, "none"), lastEventAt: null }], noon);
    expect(groups.map((g) => [g.title, g.data.map((x) => x.sessionId)])).toEqual([
      ["Today", ["t1", "t2"]], ["Yesterday", ["y"]], ["This week", ["w"]], ["Earlier", ["e", "none"]],
    ]);
    expect(sessionHistory([h(1, "t")], noon).map((g) => g.title)).toEqual(["Today"]); // empty buckets drop out
  });

  it("never marks an archived session unread", () => {
    const seen = { a: 1 };
    expect(sessionUnread(s("offline", at(1000), 5), seen, now)).toBe("unread");
    expect(sessionUnread(s("offline", at(SESSION_ARCHIVE_AFTER_MS), 5), seen, now)).toBeNull();
  });
});

describe("sendBlockedReason wording", () => {
  it("never tells a person their own session is not verified (owner ruling 2026-10-04)", () => {
    const blocked = { ...base, controlUnavailableReason: "runtime-unverified" } as LocalSession;
    expect(sendBlockedReason(blocked)).toBe("Lightbulb can't send to this session. Use its terminal on the Mac; starting the session again there turns sending back on.");
  });
});

describe("the sidebar state table (spec 2026-10-26 (e))", () => {
  const live = (o: Partial<LocalSession>) => ({ ...base, provider: "claude", sessionId: "s", status: "idle", ...o } as LocalSession);
  it.each([
    [{ status: "busy" }, "here", "Working", "Open"],
    [{ status: "idle", terminal: "held" }, "here", "Idle", "Open"],
    [{ status: "busy", terminal: "attach" }, "background", "Running in background", "Attach"],
    [{ status: "waiting-approval", terminal: "attach", waitingFor: "permission prompt" }, "waiting", "Waiting on you", "Attach"],
    [{ status: "idle", terminal: "attach", note: "run lightbulb login, then press Resume" }, "background", "Running in background", "Attach"],
    [{ status: "idle", terminal: "attach", done: true }, "done", "Done", "Attach"],
    [{ status: "offline" }, "offline", "Offline", null],
    [{ status: "ended", endedReason: "exited" }, "stopped", "Stopped", "Resume"],
    [{ status: "ended", endedOnPurpose: true }, "stopped", "Stopped", "Resume"],
  ] as const)("%o is %s: %s, %s", (fields, state, label, action) => {
    const s = live(fields as Partial<LocalSession>);
    expect(sessionState(s)).toBe(state);
    expect(sessionStateLabel(s)).toBe(label);
    expect(sessionStateAction(s)).toBe(action);
  });

  it("previews an open prompt's reason only; a hand-off note is never shown", () => {
    expect(sessionWaitingLine({ status: "waiting-approval", waitingFor: "permission prompt" })).toBe("Waiting for input: permission prompt");
    expect(sessionWaitingLine({ status: "waiting-approval" })).toBe("Waiting for input");
    expect(sessionWaitingLine({ status: "idle", note: "run lightbulb login" } as never)).toBeNull();
    expect(sessionWaitingLine({ status: "idle" })).toBeNull();
    expect(sessionWaitingLine({ status: "ended", note: "stale" } as never)).toBeNull();
  });
});

describe("sessionsNeedingYou", () => {
  const row = (humanUserId: string, status: LocalSession["status"], note?: string) => ({ humanUserId, status, note });
  const rows = [
    row("me", "waiting-approval"),
    row("me", "idle", "run lightbulb login"),
    row("me", "busy"),
    row("me", "ended", "a stale note"),
    row("other", "waiting-approval"),
  ];
  it("counts your own open prompts only, not notes, a teammate's or an ended one", () => {
    expect(sessionsNeedingYou(rows, "me")).toBe(1);
    expect(sessionsNeedingYou(rows, null)).toBe(0);
  });
  it("leaves out a kind turned off; badge-only still counts", () => {
    expect(sessionsNeedingYou(rows, "me", { ...SESSION_NOTIFICATION_DEFAULTS, waiting: "off" })).toBe(0);
    expect(sessionsNeedingYou(rows, "me", { ...SESSION_NOTIFICATION_DEFAULTS, waiting: "badge", attention: "off" })).toBe(1);
  });
});

describe("session signals", () => {
  it("say changed files, running agents and the PR's checks in the vendors' words", () => {
    expect(changesLabel({ files: 1, additions: 12, deletions: 0 })).toBe("1 file changed +12 −0");
    expect(changesLabel({ files: 3, additions: 12, deletions: 4 })).toBe("3 files changed +12 −4");
    const pr = { number: 123, state: "open" as const, url: "https://github.com/o/r/pull/123" };
    expect(sessionPrLabel({ pr })).toBe("PR #123 · open");
    expect(sessionPrLabel({ pr: { ...pr, checks: "passing" } })).toBe("PR #123 · checks passing");
    expect(sessionPrLabel({ pr: { ...pr, checks: "failing" } })).toBe("PR #123 · checks failing");
    expect(sessionPrLabel({ pr: { ...pr, checks: "pending" } })).toBe("PR #123 · checks running");
    expect(sessionPrLabel({ pr: { ...pr, state: "merged", checks: "failing" } })).toBe("PR #123 · merged");
    expect(sessionPrLabel({})).toBeNull();
    expect(sessionSignalsLine({ agents: 2, pr })).toBe("2 agents running · PR #123 · open");
    expect(sessionSignalsLine({ agents: 1 })).toBe("1 agent running");
    expect(sessionSignalsLine({})).toBeNull();
  });
});
