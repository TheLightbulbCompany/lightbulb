import { describe, expect, it } from "vitest";

import { approvalInputView, buildRows, canonicalJson, groupRows, inputDiff, lifecycleLabel, mainEvents, numberedDiff, sessionSubagents, subagentElapsedMs, subagentLabel, subagentState, subagentStatus, subagentStrip, subagentTags, backgroundWaiting, thinkingLabel, toolActivity, toolHeader, toolRowView, type TranscriptRow, pendingApproval } from "./localSessionTranscript";
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

  it("Codex CommandExecution: the END of the output, the way Codex prints it, and what is above it", () => {
    const view = toolRowView(tool([
      ev(1, { kind: "tool", role: "assistant", text: "C1", itemId: "c1", tool: { name: "CommandExecution", status: "done", input: { command: "seq 1 5" }, result: { exitCode: 0, output: "C1\nC2\nC3\nC4\nC5\n" }, lines: 5 } }),
    ]));
    expect(view).toMatchObject({ preview: "C3\nC4\nC5", full: "C1\nC2\nC3\nC4\nC5\n", more: 2, showOutput: false });
  });

  it("Codex CommandExecution cut by the desktop's size bound: the head, never the middle as its end", () => {
    const view = toolRowView(tool([
      ev(1, { kind: "tool", role: "assistant", text: "L1\nL2\nL3\nL4\nL5", itemId: "c2", truncated: true, tool: { name: "CommandExecution", status: "done", input: { command: "build" }, result: { exitCode: 0, output: "L1\nL2\nL3\nL4\nL5\nL6\nL…" }, lines: 900 } }),
    ]));
    expect(view).toMatchObject({ preview: "L1\nL2\nL3", more: 897, truncated: true });
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
    expect(toolHeader("claude", call("Edit", "src/app.ts"))).toBe("Update(src/app.ts)");
    expect(toolHeader("claude", call("Write", "notes.md"))).toBe("Write(notes.md)");
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
    const claude = toolRowView(tool([
      ev(1, { kind: "tool", role: "assistant", text: "npm test", itemId: "b1", tool: { name: "Bash", status: "running", input: { command: "npm test" } } }),
      ev(2, { kind: "tool", role: "tool", text: "P1\nP2\nP3\nP4\nP5", parentId: "b1", tool: { name: "result", status: "done", lines: 5 } }),
    ]));
    expect(claude).toMatchObject({ preview: "P1\nP2\nP3", full: "P1\nP2\nP3\nP4\nP5", more: 2 });
    // Codex with only a preview of the start: the end of what there is.
    const codex = toolRowView(tool([
      ev(1, { kind: "tool", role: "assistant", text: "P1\nP2\nP3\nP4\nP5", itemId: "c1", tool: { name: "CommandExecution", status: "done", input: { command: "make" }, lines: 5 } }),
    ]));
    expect(codex).toMatchObject({ preview: "P3\nP4\nP5", full: "P1\nP2\nP3\nP4\nP5", more: 2 });
  });
});

describe("groupRows", () => {
  const call = (seq: number, name: string, text: string, input: unknown = {}, status: "running" | "done" | "error" = "done") =>
    ev(seq, { kind: "tool", role: "assistant", text, itemId: `t${seq}`, tool: { name, status, input } });
  const bash = (seq: number, command: string, status: "running" | "done" | "error" = "done") => call(seq, "Bash", command, { command }, status);
  const kinds = (items: ReturnType<typeof groupRows>) => items.map(i => i.kind);
  const summaries = (items: ReturnType<typeof groupRows>) => items.flatMap(i => (i.kind === "tools" ? [i.summary] : []));

  it("folds every run of calls that is not an edit, one call included, and never an edit", () => {
    const items = groupRows(buildRows([
      ev(1, { kind: "text", role: "user", text: "go" }),
      call(2, "Read", "app.py"),
      ev(3, { kind: "text", role: "assistant", text: "Now the edit." }),
      call(4, "Edit", "app.py"),
      bash(5, "seq 1 14"),
      bash(6, "ls -la"),
      call(7, "Write", "notes.md"),
      ev(8, { kind: "text", role: "assistant", text: "Answer" }),
    ]), false);
    expect(kinds(items)).toEqual(["prompt", "tools", "prose", "tool", "tools", "tool", "prose"]);
    expect(summaries(items)).toEqual(["Read 1 file", "Listed 1 directory, ran 2 shell commands"]);
    expect(items[4].kind === "tools" && items[4].rows.map(r => r.seq)).toEqual([5, 6]);
  });

  it("says what a run did in Claude Code's words and order", () => {
    const items = groupRows(buildRows([
      bash(1, "git add -A"),
      bash(2, "git -c user.email=a@b.c -c user.name=spike commit -m spike"),
      ev(3, { kind: "tool", role: "tool", text: "[master 5946ee6] spike", parentId: "t2", tool: { name: "result", status: "done", result: { stdout: "[master 5946ee6] spike\n 1 file changed, 1 insertion(+), 1 deletion(-)\n", stderr: "" }, lines: 2 } }),
      bash(4, 'python3 -c "print(6*7)"'),
      bash(5, "git log --oneline"),
      call(6, "Grep", "b", { pattern: "b" }),
      call(7, "Read", "b.txt"),
    ]), false);
    expect(summaries(items)).toEqual(["Committed 5946ee6, searched for 1 pattern, read 1 file, ran 4 shell commands"]);
  });

  it("counts a failed command for what it was, not among the commands that ran", () => {
    const failedRead = groupRows(buildRows([bash(1, "cat does-not-exist.txt", "error")]), false);
    expect(summaries(failedRead)).toEqual(["Read 1 file"]);
    // Nothing but a failure still says what was attempted.
    expect(summaries(groupRows(buildRows([bash(1, "npm test", "error")]), false))).toEqual(["Ran 1 shell command"]);
  });

  it("leaves a tool it cannot name (a web fetch) as its own row, which ends a run", () => {
    const items = groupRows(buildRows([call(1, "Read", "a"), call(2, "WebFetch", "https://x.test"), call(3, "Read", "b")]), false);
    expect(kinds(items)).toEqual(["tools", "tool", "tools"]);
  });

  // The wording and order below are Claude Code 2.1.283's own, read from its fold-line code.
  it("leads with how long it thought, names the MCP servers it called, and counts a file read twice once", () => {
    const at = (s: number) => `2026-09-27T10:00:${String(s).padStart(2, "0")}.000Z`;
    const items = groupRows(buildRows([
      ev(1, { kind: "text", role: "user", text: "go", ts: at(0) }),
      ev(2, { kind: "thinking", role: "assistant", text: "", ts: at(3) }),
      call(3, "Read", "a.ts", { file_path: "a.ts" }),
      call(4, "Read", "a.ts", { file_path: "a.ts" }),
      call(5, "mcp__claude-in-chrome__navigate", ""),
      call(6, "mcp__claude-in-chrome__computer", ""),
      call(7, "mcp__ghost-os__ghost_read", ""),
      bash(8, "npm test"),
      ev(9, { kind: "text", role: "assistant", text: "Answer" }),
    ]), false);
    expect(kinds(items)).toEqual(["prompt", "tools", "prose"]);
    expect(summaries(items)).toEqual(["Thought for 3s, read 1 file, called claude-in-chrome, ghost-os 3 times, ran 1 shell command"]);
    expect(summaries(groupRows(buildRows([call(1, "mcp__github__get_issue", "")]), false))).toEqual(["Called github"]);
  });

  it("draws a thought with no calls around it as a line of its own, never under a second", () => {
    const items = groupRows(buildRows([
      ev(1, { kind: "text", role: "user", text: "go" }),
      ev(2, { kind: "thinking", role: "assistant", text: "" }),
      ev(3, { kind: "text", role: "assistant", text: "done" }),
    ]), false);
    expect(kinds(items)).toEqual(["prompt", "tools", "prose"]);
    expect(summaries(items)).toEqual(["Thought for 1s"]);
  });

  it("keeps a thought that is still running as the live last row", () => {
    const rows = buildRows([bash(1, "ls"), ev(2, { kind: "thinking", role: "assistant", text: "" })]);
    expect(kinds(groupRows(rows, true))).toEqual(["tools", "thinking"]);
    expect(summaries(groupRows(rows, false))).toEqual(["Thought for 1s, listed 1 directory, ran 1 shell command"]);
  });

  it("Codex: draws nothing for reasoning it wrote no summary of, and a summary as its own row", () => {
    const items = groupRows(buildRows([
      ev(1, { kind: "thinking", role: "assistant", text: "" }),
      ev(2, { kind: "thinking", role: "assistant", text: "" }),
      ev(3, { kind: "text", role: "assistant", text: "x" }),
      ev(4, { kind: "thinking", role: "assistant", text: "**Planning**" }),
    ]), false, "codex");
    expect(kinds(items)).toEqual(["prose", "thinking"]);
  });

  it("keeps the live last tool unfolded while the turn runs, and folds it once idle", () => {
    const rows = buildRows([bash(1, "ls"), bash(2, "pwd"), bash(3, "npm test", "running")]);
    const live = groupRows(rows, true);
    expect(kinds(live)).toEqual(["tools", "tool"]);
    expect(summaries(live)).toEqual(["Listed 1 directory, ran 2 shell commands"]);
    expect(kinds(groupRows(rows, false))).toEqual(["tools"]);
  });

  it("Codex: folds what it explored under its own lines, and draws every other command", () => {
    const run = (seq: number, command: string, parsed: unknown, status: "done" | "error" = "done", exitCode = 0) =>
      ev(seq, { kind: "tool", role: "assistant", text: "", itemId: `t${seq}`,
        tool: { name: "CommandExecution", status, input: { command, parsed: [parsed] }, result: { exitCode, output: "" }, lines: 0 } });
    const items = groupRows(buildRows([
      run(1, "sed -n '1,200p' app.py", { type: "read", name: "app.py", path: "app.py" }),
      run(2, "ls -la", { type: "list_files", path: null }),
      run(3, "rg greet src", { type: "search", query: "greet", path: "src" }),
      run(4, "seq 1 14", { type: "unknown" }),
      run(5, "cat does-not-exist.txt", { type: "read", name: "does-not-exist.txt" }, "error", 1),
    ]), false);
    expect(kinds(items)).toEqual(["tools", "tool", "tools"]);
    expect(items[0].kind === "tools" && [items[0].summary, items[0].lines]).toEqual(["Explored", ["Read app.py", "List ls -la", "Search greet in src"]]);
    expect(items[2].kind === "tools" && [items[2].summary, items[2].lines]).toEqual(["Explored · 1 failed", ["Read does-not-exist.txt (exit 1)"]]);
  });

  it("names a command by its first word when nothing recorded a verdict", () => {
    const of = (command: string) => toolActivity(bash(1, command));
    expect([of("cat a.txt"), of("ls"), of("rg foo"), of("sed -n '1,9p' a"), of("npm test")]).toEqual(["read", "list", "search", "read", "shell"]);
    // Chained, piped or redirected: just a command -- unless one link commits.
    expect([of("cat a | wc -l"), of("ls > out.txt"), of("git add -A && git commit -m x")]).toEqual(["shell", "shell", "commit"]);
  });
});

describe("edits as each terminal draws them", () => {
  const hunk = { oldStart: 1, oldLines: 5, newStart: 1, newLines: 5, lines: [" def greet(name):", '-    return "hello " + name', '+    return f"hello {name}"', " ", " def farewell(name):"] };

  it("Claude: the result line and the numbered diff, a removed line under its old number", () => {
    const view = toolRowView(tool([
      ev(1, { kind: "tool", role: "assistant", text: "app.py", itemId: "e1", tool: { name: "Edit", status: "running", input: { file_path: "app.py" } } }),
      ev(2, { kind: "tool", role: "tool", text: "", parentId: "e1", tool: { name: "result", status: "done", result: { filePath: "app.py", structuredPatch: [hunk] } } }),
    ]));
    expect(view.summary).toBe("Added 1 line, removed 1 line");
    expect(view.numbered.map(l => `${l.n}${l.mark}`)).toEqual(["1 ", "2-", "2+", "3 ", "4 "]);
    expect(view.numbered[2].text).toBe('    return f"hello {name}"');
  });

  it("Claude: a new file says how much was written", () => {
    const view = toolRowView(tool([
      ev(1, { kind: "tool", role: "assistant", text: "notes.md", itemId: "w1", tool: { name: "Write", status: "running", input: { file_path: "notes.md", contentLines: 3 } } }),
      ev(2, { kind: "tool", role: "tool", text: "", parentId: "w1", tool: { name: "result", status: "done", result: { filePath: "notes.md", structuredPatch: [] } } }),
    ]));
    expect([view.summary, view.numbered]).toEqual(["Wrote 3 lines to notes.md", []]);
  });

  it("Codex: the header carries the tally, and only the changed lines are drawn", () => {
    const edit = ev(1, { kind: "tool", role: "assistant", text: "app.py", itemId: "f1", tool: { name: "FileChange", status: "done",
      result: { "app.py": { type: "update", diff: '@@ -1,3 +1,3 @@\n def greet(name):\n-    return "hello " + name\n+    return f"hello {name}"\n \n', content: null } } } });
    expect(toolHeader("codex", edit)).toBe("Edited app.py (+1 -1)");
    expect(numberedDiff(edit.tool?.result).map(l => `${l.n}${l.mark}`)).toEqual(["2-", "2+"]);
    const add = ev(2, { kind: "tool", role: "assistant", text: "notes.md", itemId: "f2", tool: { name: "FileChange", status: "done",
      result: { "notes.md": { type: "add", diff: null, content: "- one\n- two\n- three\n" } } } });
    expect(toolHeader("codex", add)).toBe("Added notes.md (+3 -0)");
    expect(numberedDiff(add.tool?.result).map(l => `${l.n}${l.mark}${l.text}`)).toEqual(["1+- one", "2+- two", "3+- three"]);
    const many = ev(3, { kind: "tool", role: "assistant", text: "a.ts, b.ts", tool: { name: "FileChange", status: "done",
      result: { "a.ts": { type: "update", diff: "@@ -1 +1 @@\n-a\n+b" }, "b.ts": { type: "delete", content: "gone\n" } } } });
    expect(toolHeader("codex", many)).toBe("Edited 2 files (+1 -2)");
  });
});

describe("thinkingLabel", () => {
  it("times a thought as the gap since the event before it and says Thinking… while live", () => {
    const rows = buildRows([
      ev(1, { kind: "text", role: "user", text: "go", ts: "2026-09-24T10:00:00.000Z" }),
      ev(2, { kind: "thinking", role: "assistant", text: "a", ts: "2026-09-24T10:00:09.000Z" }),
      ev(3, { kind: "thinking", role: "assistant", text: "b", ts: "2026-09-24T10:00:12.000Z" }),
      ev(4, { kind: "text", role: "assistant", text: "x", ts: "2026-09-24T10:00:40.000Z" }),
    ]);
    const t = rows[1];
    expect(t.kind === "thinking" && t.text).toBe("a\nb");
    expect(t.kind === "thinking" && thinkingLabel(t, false)).toBe("Thought for 12s");
    expect(t.kind === "thinking" && thinkingLabel(t, true)).toBe("Thinking…");
    expect(thinkingLabel({ kind: "thinking", seq: 1, text: "" }, false)).toBe("Thought");
  });

  it("never times a thought across the end of a turn", () => {
    const rows = buildRows([
      ev(1, { kind: "text", role: "assistant", text: "done", ts: "2026-09-24T10:00:00.000Z" }),
      ev(2, { kind: "lifecycle", role: "system", text: "", lifecycle: "turn_end", ts: "2026-09-24T10:00:01.000Z" }),
      // The next turn's first thought, ten minutes later: the wait was not thinking.
      ev(3, { kind: "thinking", role: "assistant", text: "", ts: "2026-09-24T10:10:05.000Z" }),
    ]);
    const thought = rows.find(r => r.kind === "thinking");
    expect(thought?.kind === "thinking" && thought.ms).toBe(0);
  });

  it("never counts more than ten minutes for one thought, the terminal's own ceiling", () => {
    const rows = buildRows([
      ev(1, { kind: "text", role: "user", text: "go", ts: "2026-09-24T10:00:00.000Z" }),
      ev(2, { kind: "thinking", role: "assistant", text: "", ts: "2026-09-24T11:30:00.000Z" }),
    ]);
    expect(rows[1].kind === "thinking" && rows[1].ms).toBe(600_000);
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
    expect(toolHeader("claude", report, id => (id === "a1" ? "Survey" : undefined))).toBe("Message from @Survey");
    expect(toolHeader("claude", report)).toBe("Message from @subagent");
    const view = toolRowView(tool([report]));
    expect([view.preview, view.full, view.showOutput]).toEqual(["", "All done", true]);
  });
});

describe("subagents outside the loaded window", () => {
  const H = (h: number) => new Date(Date.UTC(2026, 8, 25, 0, 0, 0) + h * 3_600_000).toISOString();
  // The owner's case: the launch (Agent call, start marker) is on an older page and no end was ever loaded.
  const window = [
    ev(900, { kind: "text", role: "user", text: "Read   jev.py and\nlist every early return", subagent: "a96aa795930577e85", ts: H(0) }),
    ev(901, { kind: "tool", role: "assistant", text: "jev.py", itemId: "r1", subagent: "a96aa795930577e85", ts: H(0.1), tool: { name: "Read", status: "done" } }),
    ev(902, { kind: "text", role: "assistant", text: "idle chatter", ts: H(0.2) }),
  ];
  const now = Date.parse(H(22));

  it("still draws it where its first event lands, named by what it was asked, never by its id", () => {
    const rows = buildRows(window);
    expect(rows.map(r => r.kind)).toEqual(["agent", "prose"]);
    const row = rows[0].kind === "agent" ? rows[0] : null;
    expect(row?.earlier).toBe(true);
    expect(row?.subagent.description).toBe("Read jev.py and list every early return");
  });

  it("reads as Ended, with no clock, once its session is idle and it has gone quiet", () => {
    const [s] = sessionSubagents(window);
    expect(subagentStatus("claude", s, now, "idle")).toBe("Ended · 1 tool use");
    expect(subagentStatus("claude", s, now, "offline")).toBe("Ended · 1 tool use");
    // A background agent can work while its parent idles: recent activity keeps it running.
    expect(subagentState(s, "idle", Date.parse(H(0.1)) + 60_000)).toBe("running");
    expect(subagentState(s, "busy", now)).toBe("running");
  });

  it("takes its name and end from the end its own files reported", () => {
    const [s] = sessionSubagents([...window, ev(903, { kind: "lifecycle", role: "system", text: 'Agent "Survey" finished', lifecycle: "subagent_end", subagent: "a96aa795930577e85", ts: H(0.3),
      tool: { name: "Agent", status: "done", input: { description: "Survey" }, result: { status: "completed", background: false, tokens: 5400 } } })]);
    expect([s.state, s.tokens]).toEqual(["completed", 5400]);
    // A row mirrored after the end but written before it is not a resume.
    const late = sessionSubagents([ev(1, { kind: "lifecycle", role: "system", text: "", lifecycle: "subagent_end", subagent: "x", ts: H(1), tool: { name: "Agent", status: "done", result: {} } }),
      ev(2, { kind: "text", role: "assistant", text: "last words", subagent: "x", ts: H(0.9) })]);
    expect(late[0].state).toBe("completed");
    const resumed = sessionSubagents([ev(1, { kind: "lifecycle", role: "system", text: "", lifecycle: "subagent_end", subagent: "x", ts: H(1), tool: { name: "Agent", status: "done", result: {} } }),
      ev(2, { kind: "text", role: "assistant", text: "again", subagent: "x", ts: H(2) })]);
    expect(resumed[0].state).toBe("running");
  });

  it("draws a subagent whose only loaded event is its background end", () => {
    const rows = buildRows([ev(5, { kind: "lifecycle", role: "system", text: 'Agent "Old" finished', lifecycle: "subagent_end", subagent: "b1", ts: H(1),
      tool: { name: "Agent", status: "done", input: { description: "Old" }, result: { status: "completed", background: true, durationMs: 60_000 } } })]);
    expect(rows.map(r => r.kind)).toEqual(["agent", "subagent"]);
  });
});

describe("the panel above the prompt", () => {
  const T0 = Date.parse("2026-09-25T10:00:00.000Z");
  const at = (s: number) => new Date(T0 + s * 1000).toISOString();
  const running = (i: number) => [
    ev(i * 10, { kind: "lifecycle", role: "system", text: `Task ${i}`, lifecycle: "subagent_start", subagent: `s${i}`, ts: at(0), tool: { name: "Agent", status: "running", input: { subagentType: "Explore" } } }),
    ev(i * 10 + 1, { kind: "tool", role: "assistant", text: "npm test", itemId: `c${i}`, subagent: `s${i}`, ts: at(100), tool: { name: "Bash", status: "running" } }),
  ];
  const events = [1, 2, 3, 4, 5, 6, 7].flatMap(running);

  it("lists what each running subagent is doing, five at most, the rest counted", () => {
    const subs = sessionSubagents(events);
    const strip = subagentStrip("claude", subs, "busy", T0 + 130_000);
    expect(strip.more).toBe(2);
    expect(strip.rows[0]).toEqual({ id: "s1", label: "Explore", activity: "Bash(npm test)", elapsedMs: 130_000 });
    // Gone quiet under an idle session: not running, so not in the panel.
    expect(subagentStrip("claude", subs, "idle", T0 + 3_600_000).rows).toEqual([]);
  });

  it("counts background agents the finished turn is waiting for", () => {
    const bg = [
      ev(1, { kind: "tool", role: "assistant", text: "Survey", itemId: "t1", ts: at(0), tool: { name: "Agent", status: "running", input: { description: "Survey", run_in_background: true } } }),
      ev(2, { kind: "lifecycle", role: "system", text: "Survey", lifecycle: "subagent_start", subagent: "a1", parentId: "t1" }),
      ev(3, { kind: "text", role: "assistant", text: "working", subagent: "a1", ts: at(60) }),
    ];
    expect(backgroundWaiting(sessionSubagents(bg), "idle", T0 + 90_000)).toBe(1);
    expect(backgroundWaiting(sessionSubagents(bg), "idle", T0 + 3_600_000)).toBe(0);
  });
});

describe("inputDiff", () => {
  it("shows an edit's old and new lines, a write's content, nothing for bash", () => {
    expect(inputDiff("Edit", { old_string: "a\nb", new_string: "a\nc" }).map(l => l.mark + l.text)).toEqual(["-a", "-b", "+a", "+c"]);
    expect(inputDiff("MultiEdit", { edits: [{ old_string: "x", new_string: "y" }, { old_string: "p", new_string: "q" }] }).map(l => l.mark + l.text)).toEqual(["-x", "+y", "-p", "+q"]);
    expect(inputDiff("Write", { content: "x" }).map(l => l.mark + l.text)).toEqual(["+x"]);
    expect(inputDiff("Bash", { command: "ls" })).toEqual([]);
  });

  it("a running edit shows its input's diff; the result's patch replaces it once it lands", () => {
    const call = ev(1, { kind: "tool", role: "assistant", text: "src/app.ts", itemId: "t9", tool: { name: "Edit", status: "running", input: { old_string: "OLD", new_string: "NEW" } } });
    const running = toolRowView(tool([call]));
    expect(running.numbered.map(l => l.mark + l.text)).toEqual(["-OLD", "+NEW"]);
    expect(running.diff).toEqual(["-OLD", "+NEW"]);
    const done = toolRowView(tool([call, ev(2, { kind: "tool", role: "tool", text: "src/app.ts", parentId: "t9", tool: { name: "result", status: "done", result: { structuredPatch: [{ oldStart: 4, newStart: 4, lines: ["-OLD", "+NEW", " CTX"] }] } } })]));
    expect(done.numbered.map(l => `${l.n}${l.mark}${l.text}`)).toEqual(["4-OLD", "4+NEW", "5 CTX"]);
  });
});

describe("canonicalJson", () => {
  it("sorts keys at every level, compact, as the Mac digests it", async () => {
    expect(canonicalJson({ description: "d", command: "ls" })).toBe('{"command":"ls","description":"d"}');
    expect(canonicalJson({ b: null, a: [{ y: "é", x: 1 }] })).toBe('{"a":[{"x":1,"y":"é"}],"b":null}');
    // Vector computed outside JS: printf '%s' '{"command":"ls","description":"d"}' | shasum -a 256
    const bytes = await globalThis.crypto.subtle.digest("SHA-256", new TextEncoder().encode(canonicalJson({ description: "d", command: "ls" })));
    const hex = Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2, "0")).join("");
    expect(hex).toBe("b1ff34673e7684fa23bbcb26c95bae8dbdb6490caa1455565e88a5b55200813e");
  });
});

describe("approvalInputView", () => {
  it("draws a friendly view only when it renders every key, else the whole JSON", () => {
    expect(approvalInputView("Bash", { command: "ls", extra: 1 })).toEqual({ kind: "json", text: JSON.stringify({ command: "ls", extra: 1 }, null, 2) });
    expect(approvalInputView("Edit", { file_path: "/x", old_string: "a", new_string: "b", mode: "x" }).kind).toBe("json");
    expect(approvalInputView("Write", { file_path: "/x", content: 3 }).kind).toBe("json");
    expect(approvalInputView("MultiEdit", { file_path: "/x", edits: [{ old_string: "a", new_string: "b", extra: true }] }).kind).toBe("json");
    expect(approvalInputView("Bash", { command: "ls", timeout: "soon" }).kind).toBe("json");
    expect(approvalInputView("Read", { file_path: "/x" }).kind).toBe("json");
  });

  it("a Bash view shows every flag, and running outside the sandbox stands apart", () => {
    expect(approvalInputView("Bash", { command: "make", description: "build", run_in_background: true, timeout: 5000, dangerouslyDisableSandbox: true }))
      .toEqual({ kind: "bash", command: "make", outsideSandbox: true, notes: ["Description: build", "Runs in the background", "Timeout: 5000 ms"] });
  });

  it("a MultiEdit view keeps each edit apart with its own replace_all", () => {
    const view = approvalInputView("MultiEdit", { file_path: "/x", edits: [{ old_string: "a", new_string: "b" }, { old_string: "c", new_string: "d", replace_all: true }] });
    expect(view).toEqual({ kind: "edit", path: "/x", edits: [
      { lines: [{ n: null, mark: "-", text: "a" }, { n: null, mark: "+", text: "b" }], everyOccurrence: false },
      { lines: [{ n: null, mark: "-", text: "c" }, { n: null, mark: "+", text: "d" }], everyOccurrence: true },
    ] });
    expect(approvalInputView("Write", { file_path: "/x", content: "y" })).toEqual({ kind: "edit", path: "/x", edits: [{ lines: [{ n: null, mark: "+", text: "y" }], everyOccurrence: false }] });
  });
});
