import { render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { LocalSessionEvent } from "@lightbulb/shared";
import { NativeSessionTranscript } from "../NativeSessionTranscript";

const people = [{ kind: "human" as const, id: "user_a", displayName: "Aden Kim" }, { kind: "agent" as const, id: "friday", displayName: "Friday" }];
const ev = (seq: number, e: Omit<LocalSessionEvent, "seq" | "truncated"> & { truncated?: boolean }): LocalSessionEvent => ({ seq, truncated: false, ...e });

describe("NativeSessionTranscript", () => {
  const events = [
    ev(1, { kind: "text", role: "user", text: "run it", actor: { userId: "user_a" }, ts: "2026-09-12T10:00:00.000Z" }),
    ev(2, { kind: "tool", role: "assistant", text: "git status", itemId: "toolu_1", tool: { name: "Bash", status: "running", input: { command: "git status" } } }),
    ev(3, { kind: "tool", role: "tool", text: "l1\nl2", parentId: "toolu_1", tool: { name: "result", status: "done", result: { stdout: Array.from({ length: 30 }, (_, i) => `l${i + 1}`).join("\n") }, lines: 30 } }),
    ev(4, { kind: "thinking", role: "assistant", text: "" }),
    ev(5, { kind: "text", role: "assistant", text: "All good." }),
  ];
  it("draws the Claude grammar with attribution, a collapsed output that expands, and a working line", () => {
    render(<NativeSessionTranscript provider="claude" events={events} busy ownerName="Prasiddha" participants={people} />);
    expect(screen.getByText("Aden Kim")).toBeInTheDocument();
    expect(screen.getByText("Bash(git status)")).toBeInTheDocument();
    expect(screen.getByText(/\+28 lines/)).toBeInTheDocument();
    expect(screen.queryByText("l30")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /\+28 lines/ }));
    expect(screen.getByText(/l30/)).toBeInTheDocument();
    expect(screen.getByText("Thinking…")).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Working" })).toBeInTheDocument();
    expect(screen.getAllByLabelText(/Native event \d+/).length).toBe(4);
  });
  it("draws the Codex grammar and hides the working line when idle", () => {
    render(<NativeSessionTranscript provider="codex" busy={false} ownerName="Prasiddha" participants={people} events={[
      ev(1, { kind: "text", role: "user", text: "run it" }),
      ev(2, { kind: "thinking", role: "assistant", text: "**Planning**" }),
      ev(3, { kind: "tool", role: "assistant", text: "l1", itemId: "e1", tool: { name: "CommandExecution", status: "error", input: { command: "git status" }, result: { exitCode: 1, output: "l1" }, lines: 1 } }),
      ev(4, { kind: "tool", role: "assistant", text: "a.ts", itemId: "f1", tool: { name: "FileChange", status: "done", result: { "a.ts": { type: "update", diff: "-a\n+b" } } } }),
      ev(5, { kind: "text", role: "assistant", text: "Done.", itemId: "m9" }),
      ev(6, { kind: "lifecycle", role: "system", text: "", lifecycle: "turn_end" }),
    ]} />);
    expect(screen.getByText("Prasiddha")).toBeInTheDocument();
    expect(screen.getByText("Ran git status")).toBeInTheDocument();
    expect(screen.getByText("Edited a.ts")).toBeInTheDocument();
    expect(screen.getByText("+b")).toBeInTheDocument();
    expect(screen.getByText("codex")).toBeInTheDocument();
    expect(screen.queryByRole("status", { name: "Working" })).not.toBeInTheDocument();
    // Fix round 1, small item (c): thinking text renders through markdown (Codex summaries are markdown).
    expect(screen.getByText("Planning")).toBeInTheDocument();
    expect(screen.queryByText(/\*\*Planning\*\*/)).not.toBeInTheDocument();
  });
});

// Fix round 1 — review findings 1-3: exercise every result shape the Claude
// and Codex adapters actually emit, not just the Bash/FileChange-diff shapes
// the first two tests cover.
describe("adapter result shapes", () => {
  it("Claude stdout+stderr: collapses to the preview, expands to the rest", () => {
    render(<NativeSessionTranscript provider="claude" busy={false} ownerName="Prasiddha" participants={people} events={[
      ev(1, { kind: "tool", role: "assistant", text: "npm test", itemId: "t1", tool: { name: "Bash", status: "running", input: { command: "npm test" } } }),
      ev(2, { kind: "tool", role: "tool", text: "S1\nS2", parentId: "t1", tool: { name: "result", status: "done", result: { stdout: "S1\nS2\nS3", stderr: "ERRLINE", interrupted: false }, lines: 4 } }),
    ]} />);
    expect(screen.getByText(/S1/)).toBeInTheDocument();
    expect(screen.queryByText(/S3/)).not.toBeInTheDocument();
    expect(screen.queryByText(/ERRLINE/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /\+2 lines/ }));
    expect(screen.getByText(/S3/)).toBeInTheDocument();
    expect(screen.getByText(/ERRLINE/)).toBeInTheDocument();
  });

  it("Claude Edit ({filePath, structuredPatch}): renders the full diff with no expand control, filtering non-string hunk lines", () => {
    render(<NativeSessionTranscript provider="claude" busy={false} ownerName="Prasiddha" participants={people} events={[
      ev(1, { kind: "tool", role: "assistant", text: "src/app.ts", itemId: "t2", tool: { name: "Edit", status: "running", input: { file_path: "src/app.ts" } } }),
      ev(2, { kind: "tool", role: "tool", text: "src/app.ts", parentId: "t2", tool: { name: "result", status: "done", result: { filePath: "src/app.ts", structuredPatch: [{ lines: ["-OLDLINE", "+NEWLINE", " CTXLINE", null] }] } } }),
    ]} />);
    expect(screen.getByText(/NEWLINE/)).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("Claude Task ({description, status}): no preview text, a show-output button reveals both fields", () => {
    render(<NativeSessionTranscript provider="claude" busy={false} ownerName="Prasiddha" participants={people} events={[
      ev(1, { kind: "tool", role: "assistant", text: "Investigate flaky test", itemId: "t3", tool: { name: "Task", status: "running", input: {} } }),
      ev(2, { kind: "tool", role: "tool", text: "", parentId: "t3", tool: { name: "result", status: "done", result: { description: "Investigating flaky test", status: "completed" } } }),
    ]} />);
    expect(screen.queryByText(/Investigating flaky test/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "show output" }));
    expect(screen.getByText(/Investigating flaky test/)).toBeInTheDocument();
    expect(screen.getByText(/completed/)).toBeInTheDocument();
  });

  it("Claude Read ({numLines}): shows the preview and never offers a false expand into a JSON stub", () => {
    render(<NativeSessionTranscript provider="claude" busy={false} ownerName="Prasiddha" participants={people} events={[
      ev(1, { kind: "tool", role: "assistant", text: "README.md", itemId: "t4", tool: { name: "Read", status: "running", input: { file_path: "README.md" } } }),
      ev(2, { kind: "tool", role: "tool", text: "READL1\nREADL2", parentId: "t4", tool: { name: "result", status: "done", result: { numLines: 120 }, lines: 120 } }),
    ]} />);
    expect(screen.getByText(/READL1/)).toBeInTheDocument();
    expect(screen.queryByText(/numLines/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("Codex CommandExecution: collapses to the preview, expands to the rest", () => {
    render(<NativeSessionTranscript provider="codex" busy={false} ownerName="Prasiddha" participants={people} events={[
      ev(1, { kind: "tool", role: "assistant", text: "C1", itemId: "c1", tool: { name: "CommandExecution", status: "done", input: { command: "ls" }, result: { exitCode: 0, output: "C1\nC2\nC3" }, lines: 3 } }),
    ]} />);
    expect(screen.getByText(/C1/)).toBeInTheDocument();
    expect(screen.queryByText(/C2/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /\+2 lines/ }));
    expect(screen.getByText(/C2/)).toBeInTheDocument();
    expect(screen.getByText(/C3/)).toBeInTheDocument();
  });

  it("Codex FileChange add ({diff: null, content}): no diff block; a show-output button reveals the added content", () => {
    render(<NativeSessionTranscript provider="codex" busy={false} ownerName="Prasiddha" participants={people} events={[
      ev(1, { kind: "tool", role: "assistant", text: "a.ts", itemId: "f1", tool: { name: "FileChange", status: "done", result: { "a.ts": { type: "add", diff: null, content: "FADD1\nFADD2" } } } }),
    ]} />);
    expect(screen.getByText("Edited a.ts")).toBeInTheDocument();
    expect(screen.queryByText(/FADD1/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "show output" }));
    expect(screen.getByText(/FADD1/)).toBeInTheDocument();
    expect(screen.getByText(/FADD2/)).toBeInTheDocument();
  });

  it("Codex McpToolCall: the label is the header, not the output; a show-output button reveals the real result", () => {
    render(<NativeSessionTranscript provider="codex" busy={false} ownerName="Prasiddha" participants={people} events={[
      ev(1, { kind: "tool", role: "assistant", text: "db.query", itemId: "m1", tool: { name: "McpToolCall", status: "done", result: { content: [{ type: "text", text: "MCP_OK_RESULT" }] } } }),
    ]} />);
    expect(screen.getByText("Called db.query")).toBeInTheDocument();
    expect(screen.queryByText(/MCP_OK_RESULT/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "show output" }));
    expect(screen.getByText(/MCP_OK_RESULT/)).toBeInTheDocument();
  });
});

// Fix round 1 — small items (a), (b), (e), (f).
describe("small fixes", () => {
  it("strips ANSI escape codes from tool output and appends a truncated marker", () => {
    render(<NativeSessionTranscript provider="claude" busy={false} ownerName="Prasiddha" participants={people} events={[
      ev(1, { kind: "tool", role: "assistant", text: "npm run build", itemId: "a1", tool: { name: "Bash", status: "running", input: { command: "npm run build" } } }),
      ev(2, { kind: "tool", role: "tool", text: "[32mOKLINE[0m", parentId: "a1", truncated: true, tool: { name: "result", status: "done", result: { stdout: "[32mOKLINE[0m" } } }),
    ]} />);
    expect(screen.getByText(/OKLINE/)).toBeInTheDocument();
    expect(screen.queryByText(/\[32m/)).not.toBeInTheDocument();
    expect(screen.getByText(/\(truncated\)/)).toBeInTheDocument();
  });

  it("omits the lifecycle line when turn_end carries a non-numeric duration", () => {
    render(<NativeSessionTranscript provider="claude" busy={false} ownerName="Prasiddha" participants={people} events={[
      ev(1, { kind: "text", role: "user", text: "go" }),
      ev(2, { kind: "lifecycle", role: "system", text: "not-a-number", lifecycle: "turn_end" }),
    ]} />);
    expect(screen.queryByText(/Worked for/)).not.toBeInTheDocument();
    expect(screen.getAllByLabelText(/Native event \d+/).length).toBe(1);
  });

  // An old desktop or a pre-fix backend can still put explicit nulls on the wire, and
  // `"userId" in actor` / `lines !== undefined` are both true for a null.
  it("reads explicit nulls as absent: an agent follow-up is the agent, and a call's label is not its output", () => {
    render(<NativeSessionTranscript provider="claude" busy={false} ownerName="Prasiddha" participants={people} events={[
      ev(1, { kind: "text", role: "user", text: "ship it", actor: { userId: null, agentId: "friday" } as unknown as LocalSessionEvent["actor"] }),
      ev(2, { kind: "tool", role: "assistant", text: "LABEL_ONLY", itemId: "a1", tool: { name: "Bash", status: "running", input: {}, result: null, lines: null } as unknown as LocalSessionEvent["tool"] }),
    ]} />);
    expect(screen.getByText("Friday")).toBeInTheDocument();
    expect(screen.queryByText("Prasiddha")).not.toBeInTheDocument();
    expect(screen.getByText("Bash(LABEL_ONLY)")).toBeInTheDocument();
    expect(screen.queryByText(/^LABEL_ONLY$/)).not.toBeInTheDocument();
  });

  it("draws a system text event as a meta line, not a prose bullet", () => {
    render(<NativeSessionTranscript provider="claude" busy={false} ownerName="Prasiddha" participants={people} events={[
      ev(1, { kind: "text", role: "system", text: "[Lightbulb could not mirror one event: it exceeded the envelope]", truncated: true }),
    ]} />);
    const row = screen.getByLabelText("Native event 1");
    expect(row.tagName).toBe("P");
    expect(row).toHaveClass("native-session-meta");
    expect(row).toHaveTextContent("[Lightbulb could not mirror one event: it exceeded the envelope]");
  });

  it("labels an orphan tool result (no matching call) as Output rather than a raw item name", () => {
    render(<NativeSessionTranscript provider="claude" busy={false} ownerName="Prasiddha" participants={people} events={[
      ev(1, { kind: "tool", role: "tool", text: "ORPHAN_TEXT", tool: { name: "result", status: "done", result: { output: "ORPHAN_TEXT" } } }),
    ]} />);
    expect(screen.getByText("Output")).toBeInTheDocument();
    expect(screen.queryByText(/result\(/)).not.toBeInTheDocument();
  });
});
