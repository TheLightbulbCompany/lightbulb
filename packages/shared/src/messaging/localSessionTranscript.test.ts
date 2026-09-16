import { describe, expect, it } from "vitest";

import { buildRows, lifecycleLabel, toolHeader, toolRowView, type TranscriptRow } from "./localSessionTranscript";
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
