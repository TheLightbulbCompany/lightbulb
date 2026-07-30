// teamsRunDetail imports teamsData, which imports expo-router
// (useFocusEffect) at module level; stub it like the other teamsData tests.
jest.mock("expo-router", () => ({ useFocusEffect: jest.fn() }));

import {
  isActiveRunStatus,
  parseRunLogLines,
  runContextPayload,
  type RunDetail,
} from "../teamsRunDetail";

describe("parseRunLogLines", () => {
  it("extracts chunk text from NDJSON records", () => {
    const content = [
      JSON.stringify({ ts: 1, stream: "stdout", chunk: "hello" }),
      JSON.stringify({ ts: 2, stream: "stdout", chunk: "world" }),
    ].join("\n");
    expect(parseRunLogLines(content)).toEqual(["hello", "world"]);
  });

  it("splits embedded newlines inside a chunk", () => {
    const content = JSON.stringify({ chunk: "line one\nline two" });
    expect(parseRunLogLines(content)).toEqual(["line one", "line two"]);
  });

  it("keeps non-JSON lines raw and drops blanks", () => {
    expect(parseRunLogLines("plain line\n\n{not json")).toEqual([
      "plain line",
      "{not json",
    ]);
  });

  it("drops records whose chunk is not a string", () => {
    expect(parseRunLogLines(JSON.stringify({ chunk: 42 }))).toEqual([]);
  });
});

describe("isActiveRunStatus", () => {
  it.each(["running", "queued", "scheduled_retry", "RUNNING"])("%s is active", (s) => {
    expect(isActiveRunStatus(s)).toBe(true);
  });
  it.each(["succeeded", "failed", "cancelled", "timed_out", null, undefined])(
    "%s is not active",
    (s) => {
      expect(isActiveRunStatus(s)).toBe(false);
    },
  );
});

describe("runContextPayload", () => {
  const run: RunDetail = {
    id: "run_1",
    contextSnapshot: {
      issueId: "iss_1",
      taskId: "task_1",
      taskKey: "KEY-1",
      wakeCommentId: "cmt_wake",
      commentId: "cmt_plain",
      junk: "dropped",
    },
  };

  it("retry payload carries issue/task targeting only", () => {
    expect(runContextPayload(run)).toEqual({
      issueId: "iss_1",
      taskId: "task_1",
      taskKey: "KEY-1",
    });
  });

  it("resume payload prefers wakeCommentId over commentId", () => {
    expect(runContextPayload(run, { withComment: true })).toEqual({
      issueId: "iss_1",
      taskId: "task_1",
      taskKey: "KEY-1",
      commentId: "cmt_wake",
    });
  });

  it("falls through to commentId when wakeCommentId is empty", () => {
    const noWake: RunDetail = {
      id: "run_2",
      contextSnapshot: { issueId: "iss_1", wakeCommentId: "", commentId: "cmt_plain" },
    };
    expect(runContextPayload(noWake, { withComment: true })).toEqual({
      issueId: "iss_1",
      commentId: "cmt_plain",
    });
  });

  it("tolerates a missing snapshot", () => {
    expect(runContextPayload({ id: "run_3" })).toEqual({});
  });
});
