import { describe, expect, it } from "vitest";
import { mergeSessionRows, type EngineRecord } from "./sessionRow";
import type { LocalSession } from "./types";

const NET = "abc123def456/claude:conv-1";
const viewer = { userId: "me", nameOf: (id: string) => (id === "me" ? "Me" : "Teammate") };
const backend = (extra: Partial<LocalSession> = {}) =>
  ({ sessionId: "uuid-1", humanUserId: "me", provider: "claude", status: "busy", title: "Backend title", cwdName: "repo-b", ...extra }) as LocalSession;
const engine = (extra: Partial<EngineRecord> = {}): EngineRecord => ({
  session_id: NET, person: "Leo", person_id: "abc123def456", person_verified: true, computer: "leo-mac", agent: "claude",
  folder: "repo-e", repo: "lightbulb", branch: "main", status: "busy", doing: "", title: "Engine title", tier: "push", online: true, wakes: false, ...extra,
});
const one = (b: LocalSession[], e: EngineRecord[], v: typeof viewer | { userId: null } = viewer) => {
  const rows = mergeSessionRows(b, e, v);
  expect(rows).toHaveLength(1);
  return rows[0];
};

describe("mergeSessionRows", () => {
  it("joins by networkSessionId: state from the engine, actions and the owner's rename from the backend", () => {
    const b = backend({ networkSessionId: NET, status: "idle", displayTitle: "Renamed" });
    const e = engine({ status: "busy" });
    expect(one([b], [e])).toMatchObject({
      id: NET, backend: b, engine: e, source: "shared", state: "busy", title: "Renamed", agent: "claude", computer: "leo-mac",
      folder: "repo-e", repo: "lightbulb", branch: "main", person: { name: "Leo", userId: "me", isViewer: true },
    });
  });

  it("a backend-only row keeps the backend's status mapping and id", () => {
    const row = one([backend()], []);
    expect(row).toMatchObject({ id: "uuid-1", source: "shared", state: "busy", title: "Backend title", folder: "repo-b", person: { name: "Me", isViewer: true } });
    expect(row.engine).toBeUndefined();
    const states = (["idle", "unknown", "waiting-approval", "offline", "ended"] as const).map((status) => one([backend({ status })], []).state);
    expect(states).toEqual(["idle", "idle", "waiting", "offline", "ended"]);
    expect(one([backend({ status: "idle", done: true })], []).state).toBe("done");
    expect(one([backend({ status: "busy", terminal: "attach" })], []).state).toBe("busy");
    expect(one([backend({ status: "waiting-approval", waitingFor: "permission prompt" })], []).waitingFor).toBe("permission prompt");
  });

  it("keeps a row with a networkSessionId but no engine record, under that id", () => {
    expect(one([backend({ networkSessionId: NET })], [])).toMatchObject({ id: NET, source: "shared", state: "busy" });
  });

  it("an engine-only row is network, in the order after the backend's", () => {
    const row = one([], [engine()]);
    expect(row).toMatchObject({ id: NET, source: "network", state: "busy", title: "Engine title", person: { name: "Leo", isViewer: false } });
    expect(row.backend).toBeUndefined();
    expect(mergeSessionRows([backend()], [engine()], viewer).map((r) => r.source)).toEqual(["shared", "network"]);
  });

  it("falls back to the backend's folder and to New chat", () => {
    expect(one([backend({ networkSessionId: NET, cwdName: "repo-b", title: null })], [engine({ folder: "", title: "" })])).toMatchObject({ folder: "repo-b", title: "New chat" });
  });

  it("maps offline, parked, ended, waiting and done from the engine", () => {
    expect(one([], [engine({ online: false, seen_at: 880 })])).toMatchObject({ state: "offline", lastSeenAt: 880_000 });
    expect(one([], [engine({ online: false, wakes: true, seen_at: 880 })])).toMatchObject({ state: "parked", lastSeenAt: 880_000 });
    expect(one([], [engine({ status: "ended", ended_at: 900, reason: "exited" })])).toMatchObject({ state: "ended", endedAt: 900_000, endedReason: "exited" });
    expect(one([], [engine({ status: "ended", online: false, ended_at: 900 })]).state).toBe("ended");
    expect(one([], [engine({ status: "waiting", waiting_for: "permission prompt" })])).toMatchObject({ state: "waiting", waitingFor: "permission prompt" });
    for (const state of ["done", "failed", "stopped"]) expect(one([], [engine({ status: "idle", job: { state } })]).state).toBe("done");
    expect(one([], [engine({ status: "idle", job: { state: "working" } })]).state).toBe("idle");
    expect(one([], [engine({ status: "busy", job: { state: "done" } })]).state).toBe("busy");
  });

  it("drops the backend's stale ended fields once the engine says it runs", () => {
    const row = one([backend({ networkSessionId: NET, status: "ended", endedAt: "2026-10-30T00:00:00Z", endedReason: "exited" })], [engine()]);
    expect(row).toMatchObject({ state: "busy" });
    expect(row.endedAt).toBeUndefined();
    expect(row.endedReason).toBeUndefined();
  });

  it("flags the viewer by the backend's human or the engine's user, and never a signed-out viewer", () => {
    expect(one([backend()], []).person.isViewer).toBe(true);
    expect(one([backend({ humanUserId: "other" })], []).person.isViewer).toBe(false);
    expect(one([], [engine({ user_id: "me" })]).person).toEqual({ name: "Leo", userId: "me", isViewer: true });
    expect(one([], [engine({ user_id: "other" })]).person.isViewer).toBe(false);
    expect(one([], [engine()], { userId: null }).person.isViewer).toBe(false);
  });

  it("says canResume on the viewer's own ended row only", () => {
    const ended = { status: "ended", can_resume: true } as const;
    expect(one([], [engine({ ...ended, user_id: "me" })]).canResume).toBe(true);
    expect(one([], [engine({ ...ended, user_id: "other" })]).canResume).toBeUndefined();
    expect(one([backend({ humanUserId: "other", networkSessionId: NET })], [engine({ ...ended })]).canResume).toBeUndefined();
    expect(one([], [engine({ can_resume: true, user_id: "me" })]).canResume).toBeUndefined();
    expect(one([], [engine({ status: "ended", user_id: "me" })]).canResume).toBeUndefined();
  });

  it("takes a joined row's person from the backend when the two disagree", () => {
    const joined = (humanUserId: string, user_id: string) => one([backend({ humanUserId, networkSessionId: NET })], [engine({ user_id })]).person.isViewer;
    expect(joined("other", "me")).toBe(false);
    expect(joined("me", "other")).toBe(true);
  });

  it("reads a joined idle row as done when the backend says so", () => {
    expect(one([backend({ networkSessionId: NET, done: true })], [engine({ status: "idle" })]).state).toBe("done");
    expect(one([backend({ networkSessionId: NET, done: true })], [engine({ status: "busy" })]).state).toBe("busy");
  });

  it("does not take 0 for a time", () => {
    expect(one([], [engine({ online: false, seen_at: 0 })]).lastSeenAt).toBeUndefined();
    expect(one([], [engine({ status: "ended", ended_at: 0 })]).endedAt).toBeUndefined();
    const at = "2026-10-30T00:00:00Z";
    expect(one([backend({ networkSessionId: NET, lastEventAt: at })], [engine({ online: false, seen_at: 0 })]).lastSeenAt).toBe(Date.parse(at));
    expect(one([backend({ networkSessionId: NET, endedAt: at })], [engine({ status: "ended", ended_at: 0 })]).endedAt).toBe(Date.parse(at));
  });

  it("gives one row per conversation, the live one when one is", () => {
    const ended = engine({ status: "ended" });
    expect(one([], [ended, engine({ status: "idle" })]).state).toBe("idle");
    expect(one([], [engine({ status: "idle" }), ended]).state).toBe("idle");
    const stopped = backend({ sessionId: "old", networkSessionId: NET, status: "ended" });
    const live = backend({ sessionId: "new", networkSessionId: NET, status: "busy" });
    expect(one([stopped, live], []).backend?.sessionId).toBe("new");
    expect(one([live, stopped], [engine()]).backend?.sessionId).toBe("new");
  });

  it("with no engine rows keeps every backend row and its order", () => {
    const rows = [backend({ sessionId: "b" }), backend({ sessionId: "a", status: "ended" }), backend({ sessionId: "c", humanUserId: "other" })];
    expect(mergeSessionRows(rows, [], viewer).map((row) => row.backend)).toEqual(rows);
  });
});
