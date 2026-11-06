import { describe, expect, it } from "vitest";
import {
  applyRunFrame,
  buildTimeline,
  dateDividerLabel,
  lastSequence,
  mergeCanonicalMessages,
  roomDisplayName,
  runView,
  collapseRuns,
  declinedRun,
  shownRunText,
  placeRuns,
  type LiveRoomRun,
} from "./timeline";
import type { ChatIncomingMessage } from "../live/types";
import type { CanonicalRoomMessage as RoomMessage } from "./types";

const base: RoomMessage = { messageId: "m", roomId: "r", messageSeq: 0, roomRevision: 1, author: { kind: "agent", id: "friday", displayName: "Friday" }, text: "x", mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: "2026-09-11T10:00:00Z", runId: null };
const at = (seq: number, iso: string, author = base.author, extra: Partial<RoomMessage> = {}): RoomMessage => ({ ...base, messageId: `m${seq}`, messageSeq: seq, createdAt: iso, author, ...extra });
const owner = { kind: "human" as const, id: "owner", displayName: "Owner" };
// Built from local-time components (not a UTC ISO string) so this instant is
// pinned to the same local calendar day on every host timezone.
const now = new Date(2026, 8, 11, 12, 0);

describe("buildTimeline", () => {
  it("collapses consecutive same-author messages within five minutes", () => {
    const items = buildTimeline([at(1, "2026-09-11T10:00:00Z"), at(2, "2026-09-11T10:04:59Z"), at(3, "2026-09-11T10:05:00Z")], { readSeq: null, now });
    const messages = items.filter((item) => item.kind === "message");
    expect(messages.map((item) => item.kind === "message" && item.collapsed)).toEqual([false, true, true]);
  });
  it("does not collapse across authors, deletions, or a five-minute gap", () => {
    const items = buildTimeline([at(1, "2026-09-11T10:00:00Z"), at(2, "2026-09-11T10:01:00Z", owner), at(3, "2026-09-11T10:02:00Z", owner, { deletedAt: "2026-09-11T10:03:00Z" }), at(4, "2026-09-11T10:03:00Z", owner), at(5, "2026-09-11T10:09:00Z", owner)], { readSeq: null, now });
    expect(items.filter((item) => item.kind === "message").map((item) => item.kind === "message" && item.collapsed)).toEqual([false, false, false, false, false]);
  });
  it("emits one date divider per local day and never collapses across it", () => {
    // Local-time constructors, not UTC ISO literals: these two instants must
    // straddle a local calendar-day boundary on every host timezone.
    const lateYesterday = new Date(2026, 8, 10, 23, 59).toISOString();
    const earlyToday = new Date(2026, 8, 11, 0, 1).toISOString();
    const items = buildTimeline([at(1, lateYesterday), at(2, earlyToday)], { readSeq: null, now });
    expect(items.map((item) => item.kind)).toEqual(["date", "message", "date", "message"]);
    expect(items[3]).toMatchObject({ kind: "message", collapsed: false });
  });
  it("emits the New line before the first unread message and breaks grouping there", () => {
    const items = buildTimeline([at(1, "2026-09-11T10:00:00Z"), at(2, "2026-09-11T10:01:00Z"), at(3, "2026-09-11T10:02:00Z")], { readSeq: 1, now });
    expect(items.map((item) => item.kind)).toEqual(["date", "message", "new", "message", "message"]);
    expect(items[3]).toMatchObject({ kind: "message", collapsed: false });
    expect(items[4]).toMatchObject({ kind: "message", collapsed: true });
  });
  it("emits no New line when everything is read or readSeq is null", () => {
    const messages = [at(1, "2026-09-11T10:00:00Z")];
    expect(buildTimeline(messages, { readSeq: 1, now }).some((item) => item.kind === "new")).toBe(false);
    expect(buildTimeline(messages, { readSeq: null, now }).some((item) => item.kind === "new")).toBe(false);
  });
  it("gives every item a stable unique key", () => {
    const items = buildTimeline([at(1, "2026-09-11T10:00:00Z"), at(2, "2026-09-11T10:01:00Z")], { readSeq: 1, now });
    expect(new Set(items.map((item) => item.key)).size).toBe(items.length);
  });
  it("returns no items for an empty message list", () => {
    expect(buildTimeline([], { readSeq: 1, now })).toEqual([]);
  });
  it("treats an invalid createdAt as unmergeable but self-contained", () => {
    const items = buildTimeline([at(1, "2026-09-11T10:00:00Z"), at(2, "not-a-date"), at(3, "2026-09-11T10:01:00Z")], { readSeq: null, now });
    expect(items.map((item) => item.kind)).toEqual(["date", "message", "message", "message"]);
    expect(items.filter((item) => item.kind === "message").map((item) => item.kind === "message" && item.collapsed)).toEqual([false, false, false]);
  });
  it("treats readSeq 0 as a real read position, distinct from null", () => {
    const items = buildTimeline([at(1, "2026-09-11T10:00:00Z")], { readSeq: 0, now });
    expect(items.map((item) => item.kind)).toEqual(["date", "new", "message"]);
  });
});

describe("dateDividerLabel", () => {
  it("labels today, yesterday, weekday within a week, otherwise a full date", () => {
    // Local-time constructors so "today"/"yesterday" are pinned to `now`'s
    // local calendar day on every host timezone, not just hosts near UTC.
    const today = new Date(2026, 8, 11, 9);
    const yesterday = new Date(2026, 8, 10, 9);
    const withinWeek = new Date(2026, 8, 8, 9);
    const overWeek = new Date(2026, 7, 1, 9);
    expect(dateDividerLabel(today, now)).toBe("Today");
    expect(dateDividerLabel(yesterday, now)).toBe("Yesterday");
    expect(dateDividerLabel(withinWeek, now)).toBe(withinWeek.toLocaleDateString(undefined, { weekday: "long" }));
    expect(dateDividerLabel(overWeek, now)).toBe(overWeek.toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" }));
  });
});

describe("mergeCanonicalMessages", () => {
  it("keeps the newer revision of content and of reactions independently", () => {
    const a = { ...base, messageId: "m1", messageSeq: 1, messageRevision: 2, text: "new", reactionRevision: 1, reactions: {} };
    const b = { ...a, messageRevision: 1, text: "old", reactionRevision: 2, reactions: { "👍": ["u1"] } };
    expect(mergeCanonicalMessages([a], [b])).toEqual([{ ...a, reactions: { "👍": ["u1"] }, reactionRevision: 2 }]);
  });
  it("drops reactions on a deleted message and sorts by sequence", () => {
    const deleted = { ...base, messageId: "m2", messageSeq: 2, deletedAt: "now", reactions: { "👍": ["u1"] } };
    expect(mergeCanonicalMessages([deleted], [{ ...base, messageId: "m1", messageSeq: 1 }]).map((m) => m.messageSeq)).toEqual([1, 2]);
    expect(mergeCanonicalMessages([], [deleted])[0].reactions).toEqual({});
  });
  it("reports the last sequence, and zero for an empty room", () => {
    expect(lastSequence([{ ...base, messageSeq: 7 }])).toBe(7);
    expect(lastSequence([])).toBe(0);
  });
});

describe("runView", () => {
  const frame = (f: Partial<ChatIncomingMessage>) => f as ChatIncomingMessage;
  it("folds tool frames into one row per call id and keeps progress across a tool_end", () => {
    const view = runView({
      roomId: "r", agentId: "a", runId: "run1", text: "", thinking: "", state: "running",
      frames: [
        frame({ type: "tool_start", runId: "run1", tool: "bash", toolCallId: "t1", args: { cmd: "ls" } }),
        frame({ type: "command_output", runId: "run1", toolCallId: "t1", output: "a.txt" }),
        frame({ type: "tool_end", runId: "run1", tool: "bash", toolCallId: "t1", result: [{ type: "text", text: "done" }] }),
      ],
    });
    expect(view.tools).toHaveLength(1);
    expect(view.tools[0]).toMatchObject({ id: "t1", name: "bash", status: "done", progressText: "a.txt" });
  });
  it("marks a tool_error row as error and keeps activity rows separate", () => {
    const view = runView({
      roomId: "r", agentId: "a", runId: "run1", text: "", thinking: "", state: "failed",
      frames: [
        frame({ type: "tool_error", runId: "run1", tool: "bash", toolCallId: "t1", meta: "boom" }),
        frame({ type: "agent_activity", runId: "run1", activity: "plan", title: "Planning", detail: {} }),
      ],
    });
    expect(view.tools.map((t) => t.status)).toEqual(["error", "done"]);
    expect(view.tools[1].kind).toBe("activity");
  });
});

describe("roomDisplayName", () => {
  it("prefers the room name, then participants, then a kind fallback", () => {
    const p = [{ displayName: "Friday" }, { displayName: "Aden" }];
    expect(roomDisplayName({ kind: "channel", name: "Launch" }, [])).toBe("Launch");
    expect(roomDisplayName({ kind: "channel", name: "#to-do" }, [])).toBe("to-do");
    expect(roomDisplayName({ kind: "channel", name: "#" }, [])).toBe("#");
    expect(roomDisplayName({ kind: "group-dm", name: "#1 fans" }, [])).toBe("#1 fans");
    expect(roomDisplayName({ kind: "dm", name: null }, p)).toBe("Friday");
    expect(roomDisplayName({ kind: "group-dm", name: null }, p)).toBe("Friday and Aden");
    expect(roomDisplayName({ kind: "channel", name: null }, [])).toBe("Untitled project");
    expect(roomDisplayName(undefined, [])).toBe("Chat");
  });
});

describe("applyRunFrame", () => {
  const base = { roomId: "r1", agentId: "a1", runId: "run1" };
  const chunk = (seq: number | undefined, content: string, append = true) =>
    ({ ...base, frame: { type: "chunk", content, append, runId: "run1", ...(seq === undefined ? {} : { seq }) } as never });
  const fold = (payloads: ReturnType<typeof chunk>[]) =>
    payloads.reduce<LiveRoomRun | undefined>((run, payload) => applyRunFrame(run, payload), undefined)!;

  it("orders deltas by seq whatever the arrival order", () => {
    const run = fold([chunk(1, "a"), chunk(3, "c"), chunk(2, "b"), chunk(4, "d")]);
    expect(run.text).toBe("abcd");
    expect(run.frames.map((f) => (f as { seq?: number }).seq)).toEqual([1, 2, 3, 4]);
  });

  it("drops a duplicate seq and returns the same run", () => {
    const run = fold([chunk(1, "a"), chunk(2, "b")]);
    expect(applyRunFrame(run, chunk(2, "b"))).toBe(run);
  });

  it("a full-text resync with the highest seq wins even if deltas straggle in after it", () => {
    const run = fold([chunk(1, "he"), chunk(3, "hello world", false), chunk(2, "llo")]);
    expect(run.text).toBe("hello world");
  });

  it("applies seq-less frames on arrival, after everything held", () => {
    const run = fold([chunk(1, "a"), chunk(undefined, "b"), chunk(2, "c")]);
    // A seq-less frame keeps its arrival slot; seq'd frames only reorder among themselves.
    expect(run.text).toBe("abc");
  });

  it("a seq-less run_ended ends the run and a straggler never revives it", () => {
    let run = fold([chunk(1, "a")]);
    run = applyRunFrame(run, { ...base, frame: { type: "run_ended", runId: "run1", outcome: "aborted" } });
    expect(run.state).toBe("aborted");
    run = applyRunFrame(run, chunk(2, "b"));
    expect(run.state).toBe("aborted");
    run = applyRunFrame(run, { ...base, frame: { type: "error", message: "late", runId: "run1", seq: 3 } as never });
    expect(run.state).toBe("aborted");
  });

  it("a late seq-less sweeper run_ended never flips a finished run", () => {
    let run = applyRunFrame(fold([chunk(1, "a")]), { ...base, frame: { type: "run_ended", runId: "run1", outcome: "done", seq: 2 } as never });
    run = applyRunFrame(run, { ...base, frame: { type: "run_ended", runId: "run1", outcome: "error" } });
    expect(run.state).toBe("completed");
  });

  it("holds text past a seq gap until the missing frame lands, so no hole ever shows", () => {
    let run = fold([chunk(1, "was inscribed "), chunk(3, "the stone")]);
    expect(run.text).toBe("was inscribed ");
    run = applyRunFrame(run, chunk(2, "into "));
    expect(run.text).toBe("was inscribed into the stone");
  });

  it("renders through a gap that never fills once enough frames pile up past it", () => {
    const later = Array.from({ length: 16 }, (_, i) => chunk(i + 3, String.fromCharCode(99 + i)));
    const run = fold([chunk(1, "a"), ...later]);
    expect(run.text).toBe("a");
    expect(applyRunFrame(run, chunk(19, "!")).text).toBe(`a${"cdefghijklmnopqr"}!`);
  });

  it("an ended run shows everything it has, gap or not", () => {
    const run = applyRunFrame(fold([chunk(1, "a"), chunk(3, "c")]), {
      ...base,
      frame: { type: "run_ended", runId: "run1", outcome: "done", seq: 4 } as never,
    });
    expect(run.text).toBe("ac");
  });

  it("a stopped run's card stops moving: frames after the end are dropped, stragglers kept", () => {
    const tool = (seq: number, id: string) => ({ ...base, frame: { type: "tool_start", tool: "browser_navigate", toolCallId: id, runId: "run1", seq } as never });
    let run = fold([chunk(1, "a"), chunk(3, "c")]);
    run = applyRunFrame(run, { ...base, frame: { type: "run_ended", runId: "run1", outcome: "aborted", seq: 4 } as never });
    run = applyRunFrame(run, chunk(2, "b"));  // from before the end: kept
    expect(run.text).toBe("abc");
    const ended = applyRunFrame(run, tool(5, "late"));
    expect(ended).toBe(run);  // from after the end: dropped
    const forced = applyRunFrame(fold([chunk(1, "a")]), { ...base, frame: { type: "run_ended", runId: "run1", outcome: "aborted", forced: true } });
    expect(forced.forcedStop).toBe(true);
    expect(applyRunFrame(forced, tool(2, "x"))).toBe(forced);  // nothing moves after a forced stop
    // The runtime's own run_ended confirms the stop and clears the caveat.
    expect(applyRunFrame(forced, { ...base, frame: { type: "run_ended", runId: "run1", outcome: "aborted", seq: 9 } as never }).forcedStop).toBeUndefined();
  });

  it("a stopped run shows no step still running", () => {
    let run = applyRunFrame(undefined, { ...base, frame: { type: "tool_start", tool: "terminal", toolCallId: "t1", runId: "run1", seq: 1 } as never });
    expect(runView(run).tools[0].status).toBe("running");
    run = applyRunFrame(run, { ...base, frame: { type: "run_ended", runId: "run1", outcome: "aborted", seq: 2 } as never });
    expect(runView(run).tools[0].status).toBe("done");
  });

  it("an error on a live run fails it", () => {
    const run = applyRunFrame(fold([chunk(1, "a")]), { ...base, frame: { type: "error", message: "x", runId: "run1" } });
    expect(run.state).toBe("failed");
  });
});


describe("one run indicator per agent", () => {
  const view = (runId: string, extra: Partial<LiveRoomRun> = {}) => runView({
    roomId: "r", agentId: "friday", runId, triggerMessageId: `t-${runId}`, text: "", thinking: "", frames: [], state: "running", ...extra,
  });

  it("shows the run that is doing something and marks the queued one's trigger as waiting", () => {
    const queued = view("a");
    const live = view("b", { text: "Looking it up" });
    const other = view("c", { agentId: "atlas" });
    const done = view("d", { state: "completed", text: "done" });
    const { runs, waiting } = collapseRuns([queued, live, other, done]);
    expect(runs.map((run) => run.runId)).toEqual(["b", "c", "d"]);
    expect([...waiting]).toEqual([["t-a", "friday"]]);
  });

  it("keeps the first when none has progressed yet", () => {
    expect(collapseRuns([view("a"), view("b")]).runs.map((run) => run.runId)).toEqual(["a"]);
  });

  it("reads the steered receipts off the run's frames", () => {
    const frames: ChatIncomingMessage[] = [{ type: "steered", runId: "a", messageId: "m9" }];
    expect(view("a", { frames }).steeredMessageIds).toEqual(["m9"]);
    expect(view("a").steeredMessageIds).toBeUndefined();
  });
});

describe("placeRuns", () => {
  const msg = (messageSeq: number) => ({ messageId: `m${messageSeq}`, messageSeq }) as RoomMessage;
  it("keeps live runs on the tail and an ended one after the message it followed", () => {
    const messages = [msg(1), msg(2), msg(3)];
    const live = { state: "running" as const, afterSeq: 1 };
    const stopped = { state: "aborted" as const, afterSeq: 2 };
    const latest = { state: "completed" as const, afterSeq: 3 };
    const { tail, after } = placeRuns([live, stopped, latest], messages);
    expect(tail).toEqual([live, latest]);
    expect(after.get("m2")).toEqual([stopped]);
  });
});

describe("a run that declines to reply", () => {
  it("leaves no card once it ends, and never shows the marker while it streams", () => {
    expect(declinedRun({ state: "completed", text: " [SILENT]\n" })).toBe(true);
    expect(declinedRun({ state: "running", text: "[SILENT]" })).toBe(false);
    expect(declinedRun({ state: "completed", text: "[SILENT] but actually" })).toBe(false);
    expect(["[", "[SIL", "[SILENT]"].map(shownRunText)).toEqual(["", "", ""]);
    expect(shownRunText("[SILENT] is the marker")).toBe("[SILENT] is the marker");
    expect(shownRunText("On it.")).toBe("On it.");
  });
});
