import { describe, expect, it } from "vitest";

import { buildRows, groupRows, lifecycleLabel, mainEvents, sessionSubagents, subagentElapsedMs, subagentLabel, subagentStatus, subagentTags, thinkingLabel, toolHeader, toolRowView, type TranscriptRow, pendingApproval } from "./localSessionTranscript";
import type { LocalSessionEvent } from "./types";

const ev = (seq: number, e: Omit<LocalSessionEvent, "seq" | "truncated"> & { truncated?: boolean }): LocalSessionEvent => ({ seq, truncated: false, ...e });
const tool = (events: LocalSessionEvent[]) => buildRows(events).find((r): r is Extract<TranscriptRow, { kind: "tool" }> => r.kind === "tool")!;

describe("buildRows", () => {
  it("merges split prose by itemId, pairs tool results and marks the final Codex answer", () => {
    const rows = buildRows([
      ev(1, { kind: "text", role: "user", text: "run it", actor: { userId: "user_a" } }),
      ev(2, { kind: "tool", role: "assistant", text: "git status", itemId: "toolu_1", tool: { name: "Bash", status: "running", input: { command: "git status" } } }),
      ev(3, { kind: "tool", role: "tool", text: "On branch main", parentId: "toolu_1", tool: { name: "result", status: "done", result: { stdout: "On branch main\n" }, lines: 1 } }),
      ev(4, { kind: "text", role: "assistant", text: "first half ", itemId: "m1" }),
      ev(5, { kind: "text", role: "assistant", text: "second half", itemId: "m1" }),
      ev(6, { kind: "lifecycle", role: "system", text: "", lifecycle: "turn_end" }),
    ]);
    expect(rows.map(r => r.kind)).toEqual(["prompt", "tool", "prose"]); // a bare turn_end closes the turn without drawing a row
    expect(rows[0].kind === "prompt" && rows[0].who).toBe("user_a");
    expect(rows[1].kind === "tool" && rows[1].result?.text).toBe("On branch main");
    expect(rows[2].kind === "prose" && rows[2].text).toBe("first half second half");
    expect(rows[2].kind === "prose" && rows[2].final).toBe(true);
  });

  it("reads explicit nulls as absent and draws system text as a notice", () => {
    const rows = buildRows([
      ev(1, { kind: "text", role: "user", text: "ship it", actor: { userId: null, agentId: "friday" } as unknown as LocalSessionEvent["actor"] }),
      ev(2, { kind: "text", role: "system", text: "[Lightbulb could not mirror one event]", truncated: true }),
    ]);
    expect(rows[0].kind === "prompt" && rows[0].who).toBe("friday");
    expect(rows[1].kind).toBe("notice");
  });
});

describe("toolRowView", () => {
  it("Claude stdout+stderr: preview lines, full output, and the remaining line count", () => {
    const view = toolRowView(tool([
      ev(1, { kind: "tool", role: "assistant", text: "npm test", itemId: "t1", tool: { name: "Bash", status: "running", input: { command: "npm test" } } }),
      ev(2, { kind: "tool", role: "tool", text: "S1\nS2", parentId: "t1", tool: { name: "result", status: "done", result: { stdout: "S1\nS2\nS3", stderr: "ERRLINE", interrupted: false }, lines: 4 } }),
    ]));
    expect(view).toMatchObject({ status: "done", preview: "S1\nS2", full: "S1\nS2\nS3\nERRLINE", more: 2, showOutput: false, truncated: false, diff: [] });
  });

  it("Claude Edit: diff lines from structuredPatch, non-strings filtered, no expand", () => {
    const view = toolRowView(tool([
      ev(1, { kind: "tool", role: "assistant", text: "src/app.ts", itemId: "t2", tool: { name: "Edit", status: "running", input: {} } }),
      ev(2, { kind: "tool", role: "tool", text: "src/app.ts", parentId: "t2", tool: { name: "result", status: "done", result: { filePath: "src/app.ts", structuredPatch: [{ lines: ["-OLDLINE", "+NEWLINE", " CTXLINE", null] }] } } }),
    ]));
    expect(view.diff).toEqual(["-OLDLINE", "+NEWLINE", " CTXLINE"]);
    expect(view.more).toBe(0);
  });

  it("Claude Task: no preview, show-output reveals description and status", () => {
    const view = toolRowView(tool([
      ev(1, { kind: "tool", role: "assistant", text: "Investigate", itemId: "t3", tool: { name: "Task", status: "running", input: {} } }),
      ev(2, { kind: "tool", role: "tool", text: "", parentId: "t3", tool: { name: "result", status: "done", result: { description: "Investigating flaky test", status: "completed" } } }),
    ]));
    expect(view).toMatchObject({ preview: "", full: "Investigating flaky test\ncompleted", more: 0, showOutput: true });
  });

  it("Claude Read ({numLines}): preview only, never a JSON stub to expand into", () => {
    const view = toolRowView(tool([
      ev(1, { kind: "tool", role: "assistant", text: "README.md", itemId: "t4", tool: { name: "Read", status: "running", input: {} } }),
      ev(2, { kind: "tool", role: "tool", text: "READL1\nREADL2", parentId: "t4", tool: { name: "result", status: "done", result: { numLines: 120 }, lines: 120 } }),
    ]));
    expect(view).toMatchObject({ preview: "READL1\nREADL2", full: "", more: 0, showOutput: false });
  });

  it("Codex CommandExecution: the call carries the preview and the remaining count", () => {
    const view = toolRowView(tool([
      ev(1, { kind: "tool", role: "assistant", text: "C1", itemId: "c1", tool: { name: "CommandExecution", status: "done", input: { command: "ls" }, result: { exitCode: 0, output: "C1\nC2\nC3" }, lines: 3 } }),
    ]));
    expect(view).toMatchObject({ preview: "C1", full: "C1\nC2\nC3", more: 2, showOutput: false });
  });

  it("Codex FileChange add and McpToolCall: a label header, output behind show-output", () => {
    const add = toolRowView(tool([
      ev(1, { kind: "tool", role: "assistant", text: "a.ts", itemId: "f1", tool: { name: "FileChange", status: "done", result: { "a.ts": { type: "add", diff: null, content: "FADD1\nFADD2" } } } }),
    ]));
    expect(add).toMatchObject({ preview: "", full: "FADD1\nFADD2", diff: [], showOutput: true });
    const mcp = toolRowView(tool([
      ev(1, { kind: "tool", role: "assistant", text: "db.query", itemId: "m1", tool: { name: "McpToolCall", status: "done", result: { content: [{ type: "text", text: "MCP_OK_RESULT" }] } } }),
    ]));
    expect(mcp).toMatchObject({ preview: "", full: "MCP_OK_RESULT", showOutput: true });
  });

  it("strips ANSI and carries the truncated flag; a null lines field is not a preview", () => {
    const view = toolRowView(tool([
      ev(1, { kind: "tool", role: "assistant", text: "LABEL_ONLY", itemId: "a1", tool: { name: "Bash", status: "running", input: {}, result: null, lines: null } as unknown as LocalSessionEvent["tool"] }),
      ev(2, { kind: "tool", role: "tool", text: "\x1b[32mOKLINE\x1b[0m", parentId: "a1", truncated: true, tool: { name: "result", status: "done", result: { stdout: "\x1b[32mOKLINE\x1b[0m" } } }),
    ]));
    expect(view).toMatchObject({ preview: "OKLINE", full: "OKLINE", truncated: true });
    const label = toolRowView(tool([
      ev(1, { kind: "tool", role: "assistant", text: "LABEL_ONLY", itemId: "a2", tool: { name: "Bash", status: "running", input: {}, result: null, lines: null } as unknown as LocalSessionEvent["tool"] }),
    ]));
    expect(label.preview).toBe("");
  });
});

describe("toolHeader", () => {
  it("names each provider's tools the way its TUI does, and an orphan result as Output", () => {
    const call = (name: string, text: string, input?: unknown) => ev(1, { kind: "tool", role: "assistant", text, tool: { name, status: "done", input } });
    expect(toolHeader("claude", call("Bash", "git status"))).toBe("Bash(git status)");
    expect(toolHeader("codex", call("CommandExecution", "preview", { command: "ls" }))).toBe("Ran ls");
    expect(toolHeader("codex", call("FileChange", "a.ts"))).toBe("Edited a.ts");
    expect(toolHeader("codex", call("McpToolCall", "db.query"))).toBe("Called db.query");
    expect(toolHeader("claude", ev(1, { kind: "tool", role: "tool", text: "x", tool: { name: "result", status: "done" } }))).toBe("Output");
  });
});

describe("lifecycleLabel", () => {
  const row = (lifecycle: NonNullable<LocalSessionEvent["lifecycle"]>, text: string): Extract<TranscriptRow, { kind: "lifecycle" }> => ({ kind: "lifecycle", seq: 1, lifecycle, text });
  it("formats durations and names the other lifecycle events", () => {
    expect(lifecycleLabel(row("turn_end", "12000"))).toBe("Worked for 12s");
    expect(lifecycleLabel(row("turn_end", "90000"))).toBe("Worked for 1m 30s");
    expect(lifecycleLabel(row("turn_end", "not-a-number"))).toBe("");
    expect(lifecycleLabel(row("turn_aborted", ""))).toBe("Interrupted");
    expect(lifecycleLabel(row("compaction", ""))).toBe("Context compacted");
    expect(lifecycleLabel(row("approval_pending", ""))).toBe("Waiting for input");
    expect(lifecycleLabel(row("approval_pending", "Tea or coffee? (Tea / Coffee)"))).toBe("Waiting for input: Tea or coffee? (Tea / Coffee)");
    expect(lifecycleLabel(row("approval_pending", "Wants to run Bash(git push)"))).toBe("Waiting for input: Wants to run Bash(git push)");
    expect(lifecycleLabel(row("turn_start", ""))).toBe("Turn started");
  });
});

describe("toolRowView clamping", () => {
  it("clamps a long preview to three lines and expands into the rest", () => {
    const view = toolRowView(tool([
      ev(1, { kind: "tool", role: "assistant", text: "P1\nP2\nP3\nP4\nP5", itemId: "c1", tool: { name: "CommandExecution", status: "done", input: { command: "ls" }, lines: 5 } }),
    ]));
    expect(view).toMatchObject({ preview: "P1\nP2\nP3", full: "P1\nP2\nP3\nP4\nP5", more: 2 });
  });
});

describe("groupRows", () => {
  const bash = (seq: number, id: string, status: "running" | "done" | "error" = "done", name = "Bash") =>
    ev(seq, { kind: "tool", role: "assistant", text: id, itemId: id, tool: { name, status, input: {} } });
  const kinds = (items: ReturnType<typeof groupRows>) => items.map(i => i.kind);

  it("folds a run of tools (with the thinking between them) and keeps prompt and answer visible", () => {
    const items = groupRows(buildRows([
      ev(1, { kind: "text", role: "user", text: "go" }),
      ev(2, { kind: "thinking", role: "assistant", text: "plan" }),
      bash(3, "a"),
      ev(4, { kind: "thinking", role: "assistant", text: "hmm" }),
      bash(5, "b", "error"),
      bash(6, "c", "done", "Edit"),
      ev(7, { kind: "thinking", role: "assistant", text: "done" }),
      ev(8, { kind: "text", role: "assistant", text: "Answer" }),
    ]), false);
    expect(kinds(items)).toEqual(["prompt", "thinking", "tools", "thinking", "prose"]);
    const group = items[2];
    expect(group.kind === "tools" && group.seq).toBe(3);
    expect(group.kind === "tools" && group.rows.map(r => r.seq)).toEqual([3, 4, 5, 6]);
    expect(group.kind === "tools" && group.summary).toBe("Ran 3 tools · 1 edit · 1 failed");
  });

  it("leaves a single tool alone, and splits runs at prose", () => {
    const items = groupRows(buildRows([bash(1, "a"), ev(2, { kind: "text", role: "assistant", text: "x" }), bash(3, "b")]), false);
    expect(kinds(items)).toEqual(["tool", "prose", "tool"]);
  });

  it("keeps the live last tool unfolded while the turn runs, and folds it once idle", () => {
    const rows = buildRows([bash(1, "a"), bash(2, "b"), bash(3, "c", "running")]);
    const live = groupRows(rows, true);
    expect(kinds(live)).toEqual(["tools", "tool"]);
    expect(live[0].kind === "tools" && live[0].summary).toBe("Ran 2 tools");
    expect(kinds(groupRows(rows, false))).toEqual(["tools"]);
    expect(kinds(groupRows(buildRows([bash(1, "a"), bash(2, "b", "running")]), true))).toEqual(["tool", "tool"]);
  });
});

describe("thinkingLabel", () => {
  it("times a closed thinking block from the next event and says Thinking… while live", () => {
    const rows = buildRows([
      ev(1, { kind: "thinking", role: "assistant", text: "a", ts: "2026-09-24T10:00:00.000Z" }),
      ev(2, { kind: "thinking", role: "assistant", text: "b", ts: "2026-09-24T10:00:03.000Z" }),
      ev(3, { kind: "text", role: "assistant", text: "x", ts: "2026-09-24T10:00:12.000Z" }),
    ]);
    const t = rows[0];
    expect(t.kind === "thinking" && t.text).toBe("a\nb");
    expect(t.kind === "thinking" && thinkingLabel(t, false)).toBe("Thought for 12s");
    expect(t.kind === "thinking" && thinkingLabel(t, true)).toBe("Thinking…");
    expect(thinkingLabel({ kind: "thinking", seq: 1, text: "" }, false)).toBe("Thought");
  });
});

describe("pendingApproval", () => {
  const lc = (seq: number, lifecycle: string, text = "") => ({ seq, kind: "lifecycle", lifecycle, text }) as never;
  it("returns the reason only while approval_pending is the latest lifecycle", () => {
    expect(pendingApproval([lc(1, "turn_start"), lc(2, "approval_pending", "Bash(rm -rf build)")])).toBe("Bash(rm -rf build)");
    expect(pendingApproval([lc(1, "approval_pending", "x"), lc(2, "turn_end")])).toBeNull();
    expect(pendingApproval([])).toBeNull();
  });
});

describe("subagents", () => {
  const T = (s: number) => `2026-09-25T10:00:${String(s).padStart(2, "0")}.000Z`;
  const events = [
    // A background agent: the parent's call, its launch result, its own rows, then its end.
    ev(1, { kind: "tool", role: "assistant", text: "Survey", itemId: "toolu_1", ts: T(0), tool: { name: "Agent", status: "running", input: { description: "Survey", subagent_type: "Explore", run_in_background: true } } }),
    ev(2, { kind: "tool", role: "tool", text: "launched", parentId: "toolu_1", ts: T(1), tool: { name: "result", status: "done", result: { status: "async_launched", agentId: "a1" } } }),
    ev(3, { kind: "lifecycle", role: "system", text: "Survey", lifecycle: "subagent_start", subagent: "a1", parentId: "toolu_1", tool: { name: "Agent", status: "running", input: { subagentType: "Explore" } } }),
    ev(4, { kind: "text", role: "assistant", text: "child says", subagent: "a1", ts: T(2) }),
    ev(5, { kind: "lifecycle", role: "system", text: "approval", lifecycle: "approval_pending" }),
    ev(6, { kind: "lifecycle", role: "system", text: 'Agent "Survey" finished', lifecycle: "subagent_end", subagent: "a1", ts: T(40), tool: { name: "Agent", status: "done", result: { status: "completed", background: true, durationMs: 39000 } } }),
    // A foreground agent still running: known only by its call and its rows.
    ev(7, { kind: "tool", role: "assistant", text: "Fix", itemId: "toolu_2", ts: T(41), tool: { name: "Agent", status: "running", input: { description: "Fix", subagent_type: "general-purpose" } } }),
    ev(8, { kind: "lifecycle", role: "system", text: "", lifecycle: "subagent_start", subagent: "a2", parentId: "toolu_2" }),
    ev(9, { kind: "tool", role: "assistant", text: "ls", itemId: "t9", subagent: "a2", ts: T(42), tool: { name: "Bash", status: "running" } }),
    // A Codex child: its parent's activity names a path, its own rollout a nickname; it failed.
    ev(10, { kind: "lifecycle", role: "system", text: "/root/worker", lifecycle: "subagent_start", subagent: "c1", ts: T(43) }),
    ev(11, { kind: "lifecycle", role: "system", text: "Poincare", lifecycle: "subagent_start", subagent: "c1", tool: { name: "Agent", status: "running", input: { subagentType: "explorer" } } }),
    ev(12, { kind: "lifecycle", role: "system", text: "/root/worker", lifecycle: "subagent_end", subagent: "c1", ts: T(50), tool: { name: "Agent", status: "error", result: { status: "interrupted", background: false } } }),
  ];

  it("lists each with its description, type, state, clock and background flag", () => {
    const list = sessionSubagents(events);
    expect(list.map(s => [s.id, s.description, s.type, s.state, s.background])).toEqual([
      ["a1", "Survey", "Explore", "completed", true],
      ["a2", "Fix", "general-purpose", "running", false],
      ["c1", "Poincare", "explorer", "failed", null],
    ]);
    expect(subagentElapsedMs(list[0], 0)).toBe(39000);
    expect(subagentElapsedMs(list[1], Date.parse(T(51)))).toBe(10000);
    expect(subagentElapsedMs(list[2], 0)).toBe(7000);
  });

  it("draws each subagent inline where it was launched, and its rows only when expanded", () => {
    const withCodexActivity = [...events.slice(0, 9),
      ev(20, { kind: "tool", role: "assistant", text: "/root/worker", tool: { name: "SubAgentActivity", status: "done" } }),
      ...events.slice(9),
      // a1 launches its own subagent: that one draws inside a1, not in the session.
      ev(30, { kind: "tool", role: "assistant", text: "Nested", itemId: "toolu_n", subagent: "a1", tool: { name: "Agent", status: "running", input: { description: "Nested" } } }),
      ev(31, { kind: "lifecycle", role: "system", text: "Nested", lifecycle: "subagent_start", subagent: "n1", parentId: "toolu_n" }),
      ev(32, { kind: "text", role: "assistant", text: "deep", subagent: "n1" }),
    ];
    const main = buildRows(withCodexActivity);
    const shape = (rows: TranscriptRow[]) => rows.map(r => (r.kind === "agent" ? `agent:${r.subagent.id}` : r.kind === "subagent" ? `end:${r.id}` : `${r.kind}:${r.seq}`));
    // At the Agent call (its launch result folded in), the background end where the parent was told,
    // and the Codex child at its parent's `started` activity (whose own SubAgentActivity row it replaces).
    expect(shape(main)).toEqual(["agent:a1", "lifecycle:5", "end:a1", "agent:a2", "agent:c1"]);
    expect(shape(buildRows(withCodexActivity, "a1"))).toEqual(["prose:4", "agent:n1"]);
    expect(shape(buildRows(withCodexActivity, "n1"))).toEqual(["prose:32"]);
    expect(shape(buildRows(withCodexActivity, "a2"))).toEqual(["tool:9"]);
    const end = main.find(r => r.kind === "subagent");
    expect(end && subagentLabel(end)).toBe('Agent "Survey" finished · 39s');
    // A subagent's lifecycle never hides the session's own wait, and its rows are not the session's.
    expect(pendingApproval(events)).toBe("approval");
    expect(mainEvents(events).map(e => e.seq)).toEqual([1, 2, 5, 7]);
  });

  it("says what each is doing on one status line, live while it runs", () => {
    const list = sessionSubagents(events);
    expect(subagentTags(list[0])).toEqual(["Explore", "background"]);
    expect(subagentStatus("claude", list[0], 0)).toBe("Done · 39s");
    expect(subagentStatus("claude", list[1], Date.parse(T(41)) + 134_000)).toBe("Running · 2m 14s · 1 tool use · Bash(ls)");
    expect(subagentStatus("codex", list[2], 0)).toBe("Failed · 7s");
    expect(subagentStatus("claude", { ...list[0], durationMs: 19 * 60_000, toolUses: 42 }, 0)).toBe("Done · 19m · 42 tool uses");
  });

  it("still draws an Agent call no mirrored subagent answers, settled by its result", () => {
    const rows = buildRows([
      ev(1, { kind: "tool", role: "assistant", text: "Old", itemId: "t1", tool: { name: "Agent", status: "running", input: { description: "Old", subagent_type: "Explore" } } }),
      ev(2, { kind: "tool", role: "tool", text: "ok", parentId: "t1", tool: { name: "result", status: "done", result: { status: "completed" } } }),
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0].kind === "agent" && [rows[0].subagent.id, rows[0].subagent.description, rows[0].subagent.state]).toEqual(["", "Old", "completed"]);
  });
});

describe("harness rows", () => {
  it("names a subagent's hand-back by what it was asked to do", () => {
    const report = ev(1, { kind: "tool", role: "assistant", text: "", tool: { name: "AgentReport", status: "done", input: { agentId: "a1" }, result: { content: "All done" } } });
    expect(toolHeader("claude", report, id => (id === "a1" ? "Survey" : undefined))).toBe("Report from Survey");
    expect(toolHeader("claude", report)).toBe("Report from a subagent");
    const view = toolRowView(tool([report]));
    expect([view.preview, view.full, view.showOutput]).toEqual(["", "All done", true]);
  });
});
