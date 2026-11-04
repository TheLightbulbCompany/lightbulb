import { render, screen, fireEvent, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { LocalSessionEvent } from "@lightbulb/shared";
import { NativeSessionTranscript, SubagentPanel, revealSubagent } from "../NativeSessionTranscript";

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
  it("keeps raw structured tool results behind a details button", () => {
    const output = '{"message":"Successfully stopped task","command":"a very long internal command"}';
    render(<NativeSessionTranscript provider="claude" busy={false} ownerName="Alice" participants={people} events={[
      ev(1, { kind: "tool", role: "assistant", text: "Stop task", itemId: "stop1", tool: { name: "TaskStop", status: "running", input: { task_id: "task1" } } }),
      ev(2, { kind: "tool", role: "tool", parentId: "stop1", text: output, tool: { name: "result", status: "done", result: output } }),
    ]} />);
    expect(screen.queryByText(output, { exact: false })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "show output" }));
    expect(screen.getByText(output, { exact: false })).toBeInTheDocument();
  });
  it("rehydrates offline follow-ups in the same prompt style after a page reload", () => {
    render(<NativeSessionTranscript provider="claude" events={[]} busy={false} ownerName="Prasiddha" participants={people}
      outbox={[{ requestId: "saved", text: "resume when ready", chip: { label: "Queued for session", tone: "pending", detail: null } }]} />);
    const row = screen.getByRole("article", { name: "Follow-up delivery" });
    expect(within(row).getByText("resume when ready")).toBeInTheDocument();
    expect(within(row).getByText("Queued for session")).toBeInTheDocument();
  });
  it("draws the Claude grammar with attribution, a collapsed output that expands, and a working line", () => {
    render(<NativeSessionTranscript provider="claude" events={events} busy ownerName="Prasiddha" participants={people} />);
    expect(screen.getByText("Aden Kim")).toBeInTheDocument();
    // Claude Code folds even a lone shell command into one unbulleted line; opening it shows the call.
    expect(screen.queryByText("Bash(git status)")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Thought for 1s, ran 1 shell command" }));
    expect(screen.getByText("Bash(git status)")).toBeInTheDocument();
    expect(screen.getByText(/\+28 lines/)).toBeInTheDocument();
    expect(screen.queryByText("l30")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /\+28 lines/ }));
    expect(screen.getByText(/l30/)).toBeInTheDocument();
    // A thought nobody can read folds into the line, as Claude Code draws it; it is no row of its own.
    expect(screen.getAllByText(/^Thought/)).toHaveLength(1);
    expect(screen.getByRole("status", { name: "Working" })).toBeInTheDocument();
    expect(screen.getAllByLabelText(/Native event \d+/).map(row => row.getAttribute("aria-label"))).toEqual(["Native event 1", "Native event 2", "Native event 5"]);
  });
  it("draws the Codex grammar and hides the working line when idle", () => {
    const { container } = render(<NativeSessionTranscript provider="codex" busy={false} ownerName="Prasiddha" participants={people} events={[
      ev(1, { kind: "text", role: "user", text: "run it" }),
      ev(2, { kind: "thinking", role: "assistant", text: "**Planning**" }),
      ev(3, { kind: "tool", role: "assistant", text: "", itemId: "r1", tool: { name: "CommandExecution", status: "done", input: { command: "sed -n '1,200p' app.py", parsed: [{ type: "read", name: "app.py" }] }, result: { exitCode: 0, output: "" }, lines: 0 } }),
      ev(4, { kind: "tool", role: "assistant", text: "l1", itemId: "e1", tool: { name: "CommandExecution", status: "error", input: { command: "git status" }, result: { exitCode: 1, output: "l1" }, lines: 1 } }),
      ev(5, { kind: "tool", role: "assistant", text: "a.ts", itemId: "f1", tool: { name: "FileChange", status: "done", result: { "a.ts": { type: "update", diff: "-a\n+b" } } } }),
      ev(6, { kind: "text", role: "assistant", text: "Done.", itemId: "m9" }),
      ev(7, { kind: "lifecycle", role: "system", text: "", lifecycle: "turn_end" }),
    ]} />);
    expect(screen.getByText("Prasiddha")).toBeInTheDocument();
    // Codex folds only what it explored, under "Explored" with one line per command, and opens onto the calls.
    const group = screen.getByRole("button", { name: "Explored" });
    expect(group).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("Read app.py")).toBeInTheDocument();
    expect(screen.queryByText("Ran sed -n '1,200p' app.py")).not.toBeInTheDocument();
    fireEvent.click(group);
    expect(group).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Ran sed -n '1,200p' app.py")).toBeInTheDocument();
    // Every other command is its own row, and a file change carries its tally.
    expect(screen.getByText("Ran git status")).toBeInTheDocument();
    expect(screen.getByText("Edited a.ts (+1 -1)")).toBeInTheDocument();
    expect(container.querySelector('[data-diff="add"]')).toHaveTextContent("+b");
    expect(screen.queryByRole("status", { name: "Working" })).not.toBeInTheDocument();
    // Thinking is collapsed to one line; expanded, it renders through markdown (Codex summaries are markdown).
    expect(screen.queryByText("Planning")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Thought" }));
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
    fireEvent.click(screen.getByRole("button", { name: "Ran 1 shell command" }));
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

  it("a running Claude Edit draws the diff its own input describes, before any result", () => {
    render(<NativeSessionTranscript provider="claude" busy ownerName="Prasiddha" participants={people} events={[
      ev(1, { kind: "tool", role: "assistant", text: "src/app.ts", itemId: "t2", tool: { name: "Edit", status: "running", input: { file_path: "src/app.ts", old_string: "OLDLINE", new_string: "NEWLINE" } } }),
    ]} />);
    expect(screen.getByText(/OLDLINE/)).toBeInTheDocument();
    expect(screen.getByText(/NEWLINE/)).toBeInTheDocument();
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
    const fold = screen.getByRole("button", { name: "Read 1 file" });
    fireEvent.click(fold);
    expect(screen.getByText(/READL1/)).toBeInTheDocument();
    expect(screen.queryByText(/numLines/)).not.toBeInTheDocument();
    // The fold's own toggle is the only control: no expand into a JSON stub.
    expect(screen.getAllByRole("button")).toEqual([fold]);
  });

  it("Codex CommandExecution: collapses to the preview (the END of the output, as Codex prints it), expands to the rest", () => {
    render(<NativeSessionTranscript provider="codex" busy={false} ownerName="Prasiddha" participants={people} events={[
      ev(1, { kind: "tool", role: "assistant", text: "C1", itemId: "c1", tool: { name: "CommandExecution", status: "done", input: { command: "npm test" }, result: { exitCode: 0, output: "C1\nC2\nC3\nC4\nC5" }, lines: 5 } }),
    ]} />);
    expect(screen.getByText("Ran npm test")).toBeInTheDocument();
    expect(screen.getByText(/C5/)).toBeInTheDocument();
    expect(screen.queryByText(/C1/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /\+2 lines/ }));
    expect(screen.getByText(/C1/)).toBeInTheDocument();
    expect(screen.getByText(/C2/)).toBeInTheDocument();
  });

  it("Codex FileChange add ({diff: null, content}): the added content drawn as numbered added lines, under a tallied header", () => {
    const { container } = render(<NativeSessionTranscript provider="codex" busy={false} ownerName="Prasiddha" participants={people} events={[
      ev(1, { kind: "tool", role: "assistant", text: "a.ts", itemId: "f1", tool: { name: "FileChange", status: "done", result: { "a.ts": { type: "add", diff: null, content: "FADD1\nFADD2" } } } }),
    ]} />);
    expect(screen.getByText("Added a.ts (+2 -0)")).toBeInTheDocument();
    expect([...container.querySelectorAll('[data-diff="add"]')].map(line => line.textContent)).toEqual(["1+FADD1\n", "2+FADD2\n"]);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
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
    fireEvent.click(screen.getByRole("button", { name: "Ran 1 shell command" }));
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
    fireEvent.click(screen.getByRole("button", { name: "Ran 1 shell command" }));
    expect(screen.getByText("Bash(LABEL_ONLY)")).toBeInTheDocument();
    expect(screen.queryByText(/^LABEL_ONLY$/)).not.toBeInTheDocument();
  });

  it("closes a Claude turn with its glyph line, `✻ Worked for 25s`", () => {
    render(<NativeSessionTranscript provider="claude" busy={false} ownerName="Prasiddha" participants={people} events={[
      ev(1, { kind: "text", role: "user", text: "go" }),
      ev(2, { kind: "lifecycle", role: "system", text: "25000", lifecycle: "turn_end" }),
    ]} />);
    const row = screen.getByLabelText("Native event 2");
    expect(row).toHaveClass("native-session-turn-end");
    expect(row).toHaveTextContent(/^✻Worked for 25s$/);
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

describe("readability", () => {
  it("times thinking, keeps prompt and answer visible, and leaves the live tool unfolded", () => {
    render(<NativeSessionTranscript provider="claude" busy ownerName="Prasiddha" participants={people} events={[
      ev(1, { kind: "text", role: "user", text: "fix it", ts: "2026-09-24T10:00:00.000Z" }),
      ev(2, { kind: "thinking", role: "assistant", text: "look first", ts: "2026-09-24T10:00:07.000Z" }),
      ev(3, { kind: "text", role: "assistant", text: "Checking.", ts: "2026-09-24T10:00:08.000Z" }),
      ev(4, { kind: "tool", role: "assistant", text: "a", itemId: "a", tool: { name: "Read", status: "done", input: {} } }),
      ev(5, { kind: "tool", role: "assistant", text: "b", itemId: "b", tool: { name: "Edit", status: "done", input: {} } }),
      ev(6, { kind: "tool", role: "assistant", text: "npm test", itemId: "c", tool: { name: "Bash", status: "running", input: {} } }),
    ]} />);
    expect(screen.getByText("fix it")).toBeInTheDocument();
    expect(screen.getByText("Checking.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Thought for 7s" })).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("look first")).not.toBeInTheDocument();
    // The read folds into Claude's own words; an edit is never folded; the live call stays unfolded.
    expect(screen.getByRole("button", { name: "Read 1 file" })).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("Update(b)")).toBeInTheDocument();
    expect(screen.getByText("Bash(npm test)")).toBeInTheDocument();
  });

  it("clamps a diff to twelve lines until expanded", () => {
    const diff = Array.from({ length: 15 }, (_, i) => `+L${i + 1}`).join("\n");
    render(<NativeSessionTranscript provider="codex" busy={false} ownerName="Prasiddha" participants={people} events={[
      ev(1, { kind: "tool", role: "assistant", text: "a.ts", itemId: "f1", tool: { name: "FileChange", status: "done", result: { "a.ts": { type: "update", diff } } } }),
    ]} />);
    expect(screen.getByText("L12")).toBeInTheDocument();
    expect(screen.queryByText("L13")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "… +3 lines" }));
    expect(screen.getByText("L15")).toBeInTheDocument();
  });
  it("draws a subagent inline at its Agent call, opens it in place, and points its end line at it", () => {
    const scrolled: Element[] = [];
    Element.prototype.scrollIntoView = function () { scrolled.push(this); };
    render(<NativeSessionTranscript provider="claude" busy={false} ownerName="Prasiddha" participants={people} events={[
      ev(1, { kind: "tool", role: "assistant", text: "Survey", itemId: "toolu_1", ts: "2026-09-25T10:00:00.000Z", tool: { name: "Agent", status: "running", input: { description: "Survey", subagent_type: "Explore", run_in_background: true } } }),
      ev(2, { kind: "tool", role: "tool", text: "launched", parentId: "toolu_1", tool: { name: "result", status: "done", result: { status: "async_launched", agentId: "a1" } } }),
      ev(3, { kind: "lifecycle", role: "system", text: "Survey", lifecycle: "subagent_start", subagent: "a1", parentId: "toolu_1" }),
      ev(4, { kind: "tool", role: "assistant", text: "src", itemId: "t4", subagent: "a1", tool: { name: "Grep", status: "done" } }),
      ev(5, { kind: "text", role: "assistant", text: "Found three callers.", subagent: "a1" }),
      ev(6, { kind: "lifecycle", role: "system", text: 'Agent "Survey" finished', lifecycle: "subagent_end", subagent: "a1", tool: { name: "Agent", status: "done", result: { status: "completed", background: true, durationMs: 1_140_000 } } }),
      ev(7, { kind: "tool", role: "assistant", text: "", tool: { name: "AgentReport", status: "done", input: { agentId: "a1" }, result: { content: "Report body." } } }),
    ]} />);
    const toggle = screen.getByRole("button", { name: /Agent · Survey/ });
    expect(toggle).toHaveTextContent("Explore");
    expect(toggle).toHaveTextContent("background");
    expect(screen.getByText("Done · 19m · 1 tool use")).toBeInTheDocument();
    expect(screen.queryByText("Found three callers.")).not.toBeInTheDocument();
    expect(screen.queryByText("launched")).not.toBeInTheDocument();
    fireEvent.click(toggle);
    expect(screen.getByText("Found three callers.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Searched for 1 pattern" }));
    expect(screen.getByText("Grep(src)")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: 'Agent "Survey" finished · 19m' }));
    expect(scrolled).toEqual([document.querySelector('[data-subagent="a1"]')]);
    expect(screen.getByText("Message from @Survey")).toBeInTheDocument();
    expect(screen.queryByText("Report body.")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "(expand)" }));
    expect(screen.getByText("Report body.")).toBeInTheDocument();
  });
  it("pins Claude's panel of running subagents above the prompt, and a pick opens one inline", () => {
    const now = new Date().toISOString();
    const events = [
      ev(1, { kind: "tool", role: "assistant", text: "Survey", itemId: "toolu_1", ts: now, tool: { name: "Agent", status: "running", input: { description: "Survey", subagent_type: "Explore", run_in_background: true } } }),
      ev(2, { kind: "lifecycle", role: "system", text: "Survey", lifecycle: "subagent_start", subagent: "a1", parentId: "toolu_1" }),
      ev(3, { kind: "tool", role: "assistant", text: "npm test", itemId: "t3", subagent: "a1", ts: now, tool: { name: "Bash", status: "running" } }),
      ev(4, { kind: "text", role: "assistant", text: "inner words", subagent: "a1", ts: now }),
    ];
    let root: HTMLDivElement | null = null;
    render(<>
      <div ref={node => { root = node; }}><NativeSessionTranscript provider="claude" busy={false} status="idle" ownerName="P" participants={people} events={events} /></div>
      <SubagentPanel provider="claude" events={events} status="idle" onPick={id => revealSubagent(root, id)} />
    </>);
    // The parent's turn is over while a background agent works: Claude's waiting line.
    expect(screen.getByText("Waiting for 1 background agent to finish")).toBeInTheDocument();
    const panel = screen.getByRole("region", { name: "Running subagents" });
    expect(panel).toHaveTextContent("main");
    expect(panel).toHaveTextContent("Explore");
    expect(panel).toHaveTextContent("Bash(npm test)");
    expect(screen.queryByText("inner words")).not.toBeInTheDocument();
    fireEvent.click(within(panel).getByRole("button", { name: /Explore/ }));
    expect(screen.getByText("inner words")).toBeInTheDocument();
  });

  it("draws Codex its own way: a spawned agent, a Working line, and no Claude panel", () => {
    const now = new Date().toISOString();
    const events = [
      ev(1, { kind: "lifecycle", role: "system", text: "/root/worker", lifecycle: "subagent_start", subagent: "c1", ts: now }),
      ev(2, { kind: "lifecycle", role: "system", text: "Poincare", lifecycle: "subagent_start", subagent: "c1", tool: { name: "Agent", status: "running", input: { subagentType: "explorer" } } }),
      ev(3, { kind: "tool", role: "assistant", text: "", itemId: "x", ts: now, tool: { name: "CommandExecution", status: "running", input: { command: "ls" } } }),
    ];
    render(<>
      <NativeSessionTranscript provider="codex" busy status="busy" ownerName="P" participants={people} events={events} />
      <SubagentPanel provider="codex" events={events} status="busy" onPick={() => {}} />
    </>);
    expect(screen.getByRole("button", { name: /Spawned Poincare/ })).toHaveTextContent("explorer");
    expect(screen.getByRole("status", { name: "Working" })).toHaveTextContent(/^•Working/);
    expect(screen.queryByRole("region", { name: "Running subagents" })).not.toBeInTheDocument();
  });
});
