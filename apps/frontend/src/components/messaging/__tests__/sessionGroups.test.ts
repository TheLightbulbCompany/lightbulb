import { describe, expect, it } from "vitest";
import type { EngineRecord, LocalSession, SessionRow } from "@lightbulb/shared";
import type { SidebarRow } from "../roomSections";
import { foldedEntries, NEEDS_YOU_GROUP, NETWORK_GROUP, ownWaitingCount, sessionRowAt, sessionRowGroups, sessionRowKey, sessionRowMatches } from "../sessionGroups";

const NOW = Date.parse("2026-10-30T12:00:00Z");
const ago = (ms: number) => new Date(NOW - ms).toISOString();
const HOUR = 3600_000;
const DAY = 24 * HOUR;
const NO_MARKS = { pinned: [], archived: {} };
let n = 0;

function row(over: Partial<Omit<SessionRow, "person">> & { person?: Partial<SessionRow["person"]>; at?: number; seq?: number; pr?: number; offlineFor?: number } = {}): SidebarRow {
  const { person, at, seq, pr, offlineFor, ...rest } = over;
  const id = rest.id ?? `s${++n}`;
  const source = rest.source ?? "shared";
  const backend = source === "shared" ? {
    sessionId: id, lastEventAt: ago(offlineFor ?? at ?? HOUR), latestEventSeq: seq ?? 1, status: offlineFor ? "offline" : "idle",
    ...(pr ? { pr: { number: pr, state: "open", url: "" } } : {}),
  } as LocalSession : undefined;
  const merged: SessionRow = {
    id, backend, source, agent: "claude", title: `title ${id}`, state: "idle", ...rest,
    person: { name: "Me", userId: "me", isViewer: true, ...person },
  };
  return { row: merged, owner: {} as SidebarRow["owner"] };
}
const ids = (entries: SidebarRow[]) => entries.map((entry) => entry.row.id);
const run = (entries: SidebarRow[], over: Partial<Parameters<typeof sessionRowGroups>[1]> = {}) =>
  sessionRowGroups(entries, { groupBy: "person", show: "all", query: "", marks: NO_MARKS, now: NOW, ...over });
const sam = { name: "Sam", userId: "sam", isViewer: false };

describe("sessionRowGroups", () => {
  it("keeps hosted assistant connections out of the viewer's coding sessions", () => {
    const chatgpt = row({ id: "chatgpt", source: "network", agent: "chatgpt", title: "ChatGPT via Me" });
    const coding = row({ id: "coding", agent: "codex" });
    const { groups } = run([chatgpt, coding]);
    expect(ids(groups.find(g => g.id === "person:me")!.entries)).toEqual(["coding"]);
    expect(groups.find(g => g.id === "connections")).toMatchObject({ label: "Connected assistants", entries: [chatgpt] });
  });
  it("Needs you holds own waiting and failed only; a teammate's waiting stays under the teammate", () => {
    const own = row({ id: "own", state: "waiting" });
    const failed = row({ id: "failed", state: "ended", endedReason: "login-session-ended" });
    const jobFailed = row({ id: "job", state: "ended", engine: { job: { state: "failed" } } as SessionRow["engine"] });
    const plainEnded = row({ id: "plain", state: "ended" });
    const theirs = row({ id: "theirs", state: "waiting", person: sam });
    const { groups } = run([own, failed, jobFailed, plainEnded, theirs]);
    expect(groups[0].id).toBe(NEEDS_YOU_GROUP);
    expect(ids(groups[0].entries).sort()).toEqual(["failed", "job", "own"]);
    expect(groups.find((g) => g.id === "person:sam")!.entries[0].row.id).toBe("theirs");
    expect(ids(groups.find((g) => g.id === "person:me")!.entries)).toEqual(["plain"]);
  });

  it("signed-out viewer has no Needs you", () => {
    const { groups } = run([row({ state: "waiting", person: { isViewer: false, userId: undefined } })]);
    expect(groups.map((g) => g.id)).not.toContain(NEEDS_YOU_GROUP);
  });

  it("orders by recency, not busy; viewer first then A-Z", () => {
    const olderBusy = row({ id: "busy", state: "busy", at: 5 * HOUR });
    const newerIdle = row({ id: "idle", state: "idle", at: HOUR });
    const zed = row({ id: "z", person: { name: "Zed", userId: "zed", isViewer: false } });
    const ann = row({ id: "a", person: { name: "Ann", userId: "ann", isViewer: false } });
    const { groups } = run([zed, olderBusy, ann, newerIdle]);
    expect(groups.map((g) => g.label)).toEqual(["You", "Ann", "Zed"]);
    expect(ids(groups[0].entries)).toEqual(["idle", "busy"]);
  });

  it("puts a workspace member's network row under that person, not On the network", () => {
    const { groups } = run([row({ id: "net", source: "network", person: { ...sam, member: true } })], { groupBy: "person" });
    expect(groups.map((g) => g.label)).toEqual([sam.name]);
  });

  it("puts network rows in their own last group in every axis; an own network row stays under You", () => {
    const net = row({ id: "net", source: "network", person: sam, repo: "r" });
    const mine = row({ id: "mine", source: "network", repo: "r" });
    const other = row({ id: "other", repo: "r" });
    for (const groupBy of ["person", "state", "repo"] as const) {
      const { groups } = run([net, mine, other], { groupBy });
      expect(groups.at(-1)).toMatchObject({ id: NETWORK_GROUP, label: "On the network" });
      expect(ids(groups.at(-1)!.entries)).toEqual(["net"]);
      expect(groups.flatMap((g) => ids(g.entries)).sort()).toEqual(["mine", "net", "other"]);
    }
    expect(run([net, mine]).groups[0]).toMatchObject({ id: "person:me", label: "You" });
  });

  it("groups by state in order and by repo with No repo last", () => {
    const entries = [row({ id: "e", state: "ended" }), row({ id: "b", state: "busy" }), row({ id: "w", state: "waiting", person: sam }), row({ id: "i" })];
    expect(run(entries, { groupBy: "state" }).groups.map((g) => g.id)).toEqual(["state:waiting", "state:busy", "state:idle", "state:ended"]);
    const repos = [row({ id: "n" }), row({ id: "b", repo: "beta" }), row({ id: "a", repo: "alpha" }), row({ id: "f", folder: "zfolder" })];
    const { groups } = run(repos, { groupBy: "repo" });
    expect(groups.map((g) => g.label)).toEqual(["alpha", "beta", "zfolder", "No repo"]);
    expect(groups.at(-1)!.id).toBe("repo:");
  });

  it("puts pinned first, then not-gone, then gone", () => {
    const pinnedOld = row({ id: "pin", at: 9 * HOUR, state: "ended" });
    const gone = row({ id: "gone", state: "offline", at: HOUR });
    const live = row({ id: "live", at: 2 * HOUR });
    const { groups } = run([gone, live, pinnedOld], { marks: { pinned: ["pin"], archived: {} } });
    expect(ids(groups[0].entries)).toEqual(["pin", "live", "gone"]);
  });

  it("Show mine and live count what they hide; Needs you is exempt", () => {
    const entries = [row({ id: "mine" }), row({ id: "theirs", person: sam }), row({ id: "gone", state: "offline" }), row({ id: "need", state: "waiting" }),
      row({ id: "failed", state: "ended", endedReason: "login-session-ended" })];
    const mine = run(entries, { show: "mine" });
    expect(mine.hidden).toBe(1);
    expect(mine.groups.flatMap((g) => ids(g.entries)).sort()).toEqual(["failed", "gone", "mine", "need"]);
    const live = run(entries, { show: "live" });
    expect(live.hidden).toBe(1);
    expect(live.groups.flatMap((g) => ids(g.entries)).sort()).toEqual(["failed", "mine", "need", "theirs"]);
    expect(run(entries).hidden).toBe(0);
  });

  it("filters by query, Needs you included", () => {
    const entries = [row({ id: "pr", pr: 4527 }), row({ id: "w", state: "waiting" }), row({ id: "x", title: "fix (parser)" })];
    const found = (query: string) => run(entries, { query }).groups.flatMap((g) => ids(g.entries));
    expect(found("#4527")).toEqual(["pr"]);
    expect(found("(")).toEqual(["x"]);
    expect(found("waiting")).toEqual(["w"]);
    expect(found("nothing")).toEqual([]);
    expect(sessionRowMatches(entries[0].row, "")).toBe(true);
    expect(sessionRowMatches(row({ state: "busy" }).row, "working")).toBe(true);
    expect(sessionRowMatches(row({ state: "ended", endedReason: "login-session-ended" }).row, "failed")).toBe(true);
  });

  it("archives by the viewer's mark, brings it back on new output, and never archives own waiting", () => {
    const marks = { pinned: [], archived: { a: 3, w: 3 } };
    const a = row({ id: "a", seq: 3 });
    const w = row({ id: "w", seq: 3, state: "waiting" });
    const r = run([a, w], { marks, query: "zzz" });
    expect(ids(r.archived)).toEqual(["a"]);
    expect(run([row({ id: "a", seq: 4 })], { marks }).archived).toEqual([]);
    expect(run([a], { marks }).listed).toBe(1);
  });

  it("drops a backend-only row offline for 3 days, but not one with an engine record", () => {
    const old = row({ id: "old", offlineFor: 3 * DAY + HOUR, state: "offline" });
    const known = row({ id: "known", offlineFor: 3 * DAY + HOUR, state: "offline", engine: {} as SessionRow["engine"] });
    const r = run([old, known]);
    expect(r.listed).toBe(1);
    expect(r.groups.flatMap((g) => ids(g.entries))).toEqual(["known"]);
    expect(r.archived).toEqual([]);
  });
});

describe("helpers", () => {
  it("keeps pinned past the fold limit, and limit 0 shows only kept", () => {
    const entries = ["a", "b", "c", "d"].map((id) => row({ id }));
    const keep = (entry: SidebarRow) => entry.row.id === "d";
    expect(ids(foldedEntries(entries, { limit: 2, keep }))).toEqual(["a", "b", "d"]);
    expect(ids(foldedEntries(entries, { limit: 0, keep }))).toEqual(["d"]);
    expect(ids(foldedEntries(entries, { limit: 9, keep: () => false }))).toEqual(["a", "b", "c", "d"]);
  });

  it("counts own waiting rows", () => {
    expect(ownWaitingCount([row({ state: "waiting" }).row, row({ state: "waiting", person: sam }).row, row({ state: "busy" }).row])).toBe(1);
  });

  it("keys by backend session id and times by backend event, else row times", () => {
    expect(sessionRowKey(row({ id: "net" }).row)).toBe("net");
    expect(sessionRowKey({ ...row({ id: "net" }).row, id: "other" })).toBe("net");
    expect(sessionRowKey(row({ source: "network", id: "e" }).row)).toBe("e");
    expect(sessionRowAt(row({ at: HOUR }).row)).toBe(Date.parse(ago(HOUR)));
    expect(sessionRowAt(row({ source: "network", endedAt: 7, lastSeenAt: 9 }).row)).toBe(7);
    expect(sessionRowAt(row({ source: "network" }).row)).toBe(0);
    // A live engine-only row dates from when the network last saw it.
    expect(sessionRowAt({ ...row({ source: "network" }).row, engine: { seen_at: 5 } as EngineRecord })).toBe(5000);
  });
});
