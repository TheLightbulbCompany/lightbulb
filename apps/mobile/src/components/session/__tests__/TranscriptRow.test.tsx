import { fireEvent, render, screen } from "@testing-library/react-native";
import { StyleSheet, type StyleProp, type TextStyle } from "react-native";
import { buildRows, groupRows, type LocalSessionEvent } from "@lightbulb/shared";

import { PendingPromptRow, TranscriptRowView, WorkingRow } from "../TranscriptRow";

const ev = (seq: number, e: Omit<LocalSessionEvent, "seq" | "truncated"> & { truncated?: boolean }): LocalSessionEvent => ({ seq, truncated: false, ...e });
const name = (who: string | null) => (who === "friday" ? "Friday" : who ? "Teammate" : "Prasiddha");
const draw = (provider: "claude" | "codex", events: LocalSessionEvent[]) =>
  render(<>{buildRows(events).map((row) => <TranscriptRowView key={row.seq} provider={provider} row={row} name={name} />)}</>);

describe("TranscriptRowView", () => {
  it("draws a teammate's prompt as a left bubble with attribution, prose and a thought chip", async () => {
    await draw("claude", [
      ev(1, { kind: "text", role: "user", text: "run it", actor: { agentId: "friday" }, ts: "2026-09-12T10:00:00.000Z" }),
      ev(2, { kind: "thinking", role: "assistant", text: "" }),
      ev(3, { kind: "text", role: "assistant", text: "All green.", itemId: "m1" }),
    ]);
    expect(screen.queryByText("❯")).toBeNull();
    expect(screen.getByText("run it")).toBeTruthy();
    expect(screen.getByText("Friday")).toBeTruthy();
    expect(screen.getByLabelText("Thought")).toBeTruthy();
    expect(screen.getByText("All green.")).toBeTruthy();
  });

  it("draws the viewer's own prompt as a mine bubble", async () => {
    await render(<TranscriptRowView provider="claude" row={buildRows([ev(1, { kind: "text", role: "user", text: "ship it" })])[0]} name={name} isMine={(who) => who === null} />);
    expect(screen.getByLabelText("You: ship it")).toBeTruthy();
  });

  it("attributes an expanded subagent's own prompt to the parent agent, not the viewer", async () => {
    const events: LocalSessionEvent[] = [
      ev(1, { kind: "tool", role: "assistant", text: "Fix", itemId: "toolu_1", tool: { name: "Agent", status: "running", input: { description: "Fix", subagent_type: "worker" } } }),
      ev(2, { kind: "lifecycle", role: "system", text: "", lifecycle: "subagent_start", subagent: "a1", parentId: "toolu_1" }),
      // No `actor` — buildRows reads this as `who: null`, meaning the PARENT AGENT wrote it, not the viewer.
      ev(3, { kind: "text", role: "user", text: "do the thing", subagent: "a1" }),
    ];
    const rows = buildRows(events);
    // `isMine` here mirrors the session screen's own-session case, where a top-level `who === null`
    // prompt (the session owner) IS the viewer — the nested subagent prompt must not inherit that.
    await render(<>{rows.map((row) => <TranscriptRowView key={row.seq} provider="claude" row={row} name={name} isMine={(who) => who === null} events={events} />)}</>);
    await fireEvent.press(screen.getByText(/^Agent · Fix/));
    expect(screen.getByText("Parent agent")).toBeTruthy();
    expect(screen.getByLabelText("Parent agent: do the thing")).toBeTruthy();
    expect(screen.queryByLabelText("You: do the thing")).toBeNull();
  });

  it("collapses thinking to one line and folds a run of tools into a summary that expands", async () => {
    const rows = groupRows(buildRows([
      ev(1, { kind: "text", role: "user", text: "go", ts: "2026-09-24T10:00:00.000Z" }),
      // A thought is timed as the gap since the event before it, as Claude Code times it.
      ev(2, { kind: "thinking", role: "assistant", text: "SECRET_PLAN", ts: "2026-09-24T10:00:05.000Z" }),
      ev(3, { kind: "tool", role: "assistant", text: "a.ts", itemId: "a", tool: { name: "Edit", status: "done", input: {} } }),
      ev(4, { kind: "tool", role: "assistant", text: "npm test", itemId: "b", tool: { name: "Bash", status: "done", input: {} } }),
    ]), false, "claude");
    await render(<>{rows.map((row) => <TranscriptRowView key={row.seq} provider="claude" row={row} name={name} />)}</>);
    expect(screen.queryByText(/SECRET_PLAN/)).toBeNull();
    await fireEvent.press(screen.getByLabelText("Thought for 5s"));
    expect(screen.getByText(/SECRET_PLAN/)).toBeTruthy();
    // An edit is never folded; the command after it is, even alone.
    expect(screen.getByText("Update(a.ts)")).toBeTruthy();
    const group = screen.getByLabelText("Ran 1 shell command");
    expect(group.props.accessibilityState).toEqual({ expanded: false });
    expect(screen.queryByText("Bash(npm test)")).toBeNull();
    await fireEvent.press(group);
    expect(screen.getByText("Bash(npm test)")).toBeTruthy();
  });

  it("Codex: shows what it explored under the chip without opening it", async () => {
    const run = (seq: number, command: string, parsed: unknown) =>
      ev(seq, { kind: "tool", role: "assistant", text: "", itemId: `t${seq}`, tool: { name: "CommandExecution", status: "done", input: { command, parsed: [parsed] }, result: { exitCode: 0, output: "" }, lines: 0 } });
    const rows = groupRows(buildRows([run(1, "sed -n '1,9p' app.py", { type: "read", name: "app.py" }), run(2, "ls -la", { type: "list_files" })]), false, "codex");
    await render(<>{rows.map((row) => <TranscriptRowView key={row.seq} provider="codex" row={row} name={name} />)}</>);
    expect(screen.getByText("Read app.py")).toBeTruthy();
    expect(screen.getByText("List ls -la")).toBeTruthy();
  });

  it("Codex: a subagent's reasoning with no summary draws nothing, as at the top level", async () => {
    const events: LocalSessionEvent[] = [
      ev(1, { kind: "tool", role: "assistant", text: "Fix", itemId: "toolu_1", tool: { name: "Agent", status: "running", input: { description: "Fix" } } }),
      ev(2, { kind: "lifecycle", role: "system", text: "", lifecycle: "subagent_start", subagent: "a1", parentId: "toolu_1" }),
      ev(3, { kind: "thinking", role: "assistant", text: "", subagent: "a1" }),
      ev(4, { kind: "text", role: "assistant", text: "fixed it", subagent: "a1" }),
    ];
    await render(<>{buildRows(events).map((row) => <TranscriptRowView key={row.seq} provider="codex" row={row} name={name} events={events} />)}</>);
    await fireEvent.press(screen.getByText(/^Spawned Fix/));
    expect(screen.getByText("fixed it")).toBeTruthy();
    expect(screen.queryByText(/^Thought/)).toBeNull();
  });

  it("draws a thought with no calls as a muted line, not a tool chip", async () => {
    const rows = groupRows(buildRows([
      ev(1, { kind: "text", role: "user", text: "go", ts: "2026-09-24T10:00:00.000Z" }),
      ev(2, { kind: "thinking", role: "assistant", text: "", ts: "2026-09-24T10:00:04.000Z" }),
      ev(3, { kind: "text", role: "assistant", text: "Done.", itemId: "m1" }),
    ]), false, "claude");
    await render(<>{rows.map((row) => <TranscriptRowView key={row.seq} provider="claude" row={row} name={name} />)}</>);
    expect(screen.getByText("Thought for 4s")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Thought for 4s/ })).toBeNull();
  });

  it("says Thinking… for a live thinking row", async () => {
    await render(<TranscriptRowView provider="claude" row={{ kind: "thinking", seq: 1, text: "x" }} name={name} live />);
    expect(screen.getByLabelText("Thinking…")).toBeTruthy();
  });

  it("collapses tool output to the preview and expands the rest on tap", async () => {
    await draw("claude", [
      ev(1, { kind: "tool", role: "assistant", text: "npm test", itemId: "t1", tool: { name: "Bash", status: "running", input: { command: "npm test" } } }),
      ev(2, { kind: "tool", role: "tool", text: "S1\nS2", parentId: "t1", tool: { name: "result", status: "done", result: { stdout: "S1\nS2\nS3" }, lines: 3 } }),
    ]);
    expect(screen.getByText("Bash(npm test)")).toBeTruthy();
    await fireEvent.press(screen.getByLabelText("Bash(npm test)"));
    expect(screen.getByText(/S1/)).toBeTruthy();
    expect(screen.queryByText(/S3/)).toBeNull();
    expect(screen.getByLabelText("… +1 lines").props.accessibilityState).toEqual({ expanded: false });
    await fireEvent.press(screen.getByText("… +1 lines"));
    expect(screen.getByText(/S3/)).toBeTruthy();
    expect(screen.getByLabelText("collapse").props.accessibilityState).toEqual({ expanded: true });
    await fireEvent.press(screen.getByText("collapse"));
    expect(screen.queryByText(/S3/)).toBeNull();
  });

  it("draws a diff line by line, the truncated marker, an error line and a Codex final answer", async () => {
    await draw("codex", [
      ev(1, { kind: "tool", role: "assistant", text: "a.ts", itemId: "f1", truncated: true, tool: { name: "FileChange", status: "error", result: { "a.ts": { type: "update", diff: "-OLD\n+NEW\n CTX" } } } }),
      ev(2, { kind: "text", role: "assistant", text: "Done.", itemId: "m1" }),
      ev(3, { kind: "lifecycle", role: "system", text: "12000", lifecycle: "turn_end" }),
    ]);
    expect(screen.getByText("Edited a.ts (+1 -1)")).toBeTruthy();
    expect(screen.getByText("Error")).toBeTruthy();
    expect(screen.getByText("+NEW")).toBeTruthy();
    expect(screen.getByText("-OLD")).toBeTruthy();
    expect(screen.getByText("(truncated)")).toBeTruthy();
    expect(screen.getByText("final answer")).toBeTruthy();
    expect(screen.getByText("Worked for 12s")).toBeTruthy();
  });

  it("shows a working line with elapsed seconds", async () => {
    await render(<WorkingRow provider="claude" since={new Date(Date.now() - 4000).toISOString()} />);
    expect(screen.getByLabelText("Working")).toBeTruthy();
    expect(screen.getByText(/Working · \d+s/)).toBeTruthy();
  });

  it("uses no monospace outside code", async () => {
    await draw("claude", [
      ev(1, { kind: "text", role: "user", text: "go" }),
      ev(2, { kind: "tool", role: "assistant", text: "ls", itemId: "t", tool: { name: "Bash", status: "done", input: { command: "ls" } } }),
    ]);
    // RNTL v14 dropped `UNSAFE_root`/`findAll` (test-renderer no longer exposes
    // them); walk the rendered JSON tree by hand instead.
    const monoNodes: unknown[] = [];
    const walk = (node: unknown): void => {
      if (!node || typeof node !== "object") return;
      const { props, children } = node as { props?: { style?: StyleProp<TextStyle> }; children?: unknown };
      if (props?.style && StyleSheet.flatten(props.style)?.fontFamily === "Menlo") monoNodes.push(node);
      if (Array.isArray(children)) children.forEach(walk);
    };
    walk(screen.toJSON());
    expect(monoNodes).toHaveLength(0); // output well is collapsed; nothing else may be mono
  });
});

describe("PendingPromptRow", () => {
  it("draws the unsent follow-up greyed with one status chip", async () => {
    await render(<PendingPromptRow provider="claude" text="run the tests" ownerName="You"
      chip={{ label: "Queued on your Mac", tone: "pending", detail: "Admission is not completed work." }} />);
    expect(screen.queryByText("❯")).toBeNull();
    expect(screen.getByLabelText("Pending follow-up")).toBeTruthy();
    expect(screen.getByText("run the tests")).toBeTruthy();
    expect(screen.getByText("Queued on your Mac")).toBeTruthy();
    expect(screen.getByLabelText("Queued on your Mac. Admission is not completed work.")).toBeTruthy();
  });
});
