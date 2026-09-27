import { fireEvent, render, screen } from "@testing-library/react-native";
import { buildRows, groupRows, type LocalSessionEvent } from "@lightbulb/shared";

import { PendingPromptRow, TranscriptRowView, WorkingRow } from "../TranscriptRow";

const ev = (seq: number, e: Omit<LocalSessionEvent, "seq" | "truncated"> & { truncated?: boolean }): LocalSessionEvent => ({ seq, truncated: false, ...e });
const name = (who: string | null) => (who === "friday" ? "Friday" : who ? "Teammate" : "Prasiddha");
const draw = (provider: "claude" | "codex", events: LocalSessionEvent[]) =>
  render(<>{buildRows(events).map((row) => <TranscriptRowView key={row.seq} provider={provider} row={row} name={name} />)}</>);

describe("TranscriptRowView", () => {
  it("draws a prompt with the Claude glyph and attribution, prose and a thinking marker", async () => {
    await draw("claude", [
      ev(1, { kind: "text", role: "user", text: "run it", actor: { agentId: "friday" }, ts: "2026-09-12T10:00:00.000Z" }),
      ev(2, { kind: "thinking", role: "assistant", text: "weighing it" }),
      ev(3, { kind: "text", role: "assistant", text: "All green.", itemId: "m1" }),
    ]);
    expect(screen.getByText("❯")).toBeTruthy();
    expect(screen.getByText("run it")).toBeTruthy();
    expect(screen.getByText(/^Friday · /)).toBeTruthy();
    expect(screen.getByLabelText("Thought")).toBeTruthy(); // closed by the answer, untimed
    expect(screen.getByText("All green.")).toBeTruthy();
  });

  it("draws a thought nobody can read as one plain line, as Claude Code does", async () => {
    const rows = groupRows(buildRows([
      ev(1, { kind: "text", role: "user", text: "go", ts: "2026-09-24T10:00:00.000Z" }),
      ev(2, { kind: "thinking", role: "assistant", text: "", ts: "2026-09-24T10:00:04.000Z" }),
      ev(3, { kind: "text", role: "assistant", text: "All green.", itemId: "m1", ts: "2026-09-24T10:00:05.000Z" }),
    ]), false, "claude");
    await render(<>{rows.map((row) => <TranscriptRowView key={row.seq} provider="claude" row={row} name={name} />)}</>);
    expect(screen.getByText("All green.")).toBeTruthy();
    expect(screen.getByText("Thought for 4s")).toBeTruthy();
    expect(screen.queryByText("✻", { includeHiddenElements: true })).toBeNull();
  });

  it("collapses thinking to one line and folds a run of tools into a summary that expands", async () => {
    const rows = groupRows(buildRows([
      ev(1, { kind: "text", role: "user", text: "go", ts: "2026-09-24T10:00:00.000Z" }),
      ev(2, { kind: "thinking", role: "assistant", text: "SECRET_PLAN", ts: "2026-09-24T10:00:05.000Z" }),
      ev(3, { kind: "tool", role: "assistant", text: "a.ts", itemId: "a", tool: { name: "Edit", status: "done", input: {} } }),
      ev(4, { kind: "tool", role: "assistant", text: "npm test", itemId: "b", tool: { name: "Bash", status: "done", input: {} } }),
    ]), false);
    await render(<>{rows.map((row) => <TranscriptRowView key={row.seq} provider="claude" row={row} name={name} />)}</>);
    expect(screen.queryByText(/SECRET_PLAN/)).toBeNull();
    await fireEvent.press(screen.getByLabelText("Thought for 5s"));
    expect(screen.getByText(/SECRET_PLAN/)).toBeTruthy();
    // The edit is never folded; the command after it is, even alone.
    expect(screen.getByText("Update(a.ts)")).toBeTruthy();
    const group = screen.getByLabelText("Ran 1 shell command");
    expect(group.props.accessibilityState).toEqual({ expanded: false });
    expect(screen.queryByText("Bash(npm test)")).toBeNull();
    await fireEvent.press(group);
    expect(screen.getByText("Bash(npm test)")).toBeTruthy();
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
    expect(screen.getByText(/S1/)).toBeTruthy();
    expect(screen.queryByText(/S3/)).toBeNull();
    await fireEvent.press(screen.getByText("… +1 lines"));
    expect(screen.getByText(/S3/)).toBeTruthy();
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
    expect(screen.getByText("└ Error")).toBeTruthy();
    expect(screen.getByText("+NEW")).toBeTruthy();
    expect(screen.getByText("-OLD")).toBeTruthy();
    expect(screen.getByText("(truncated)")).toBeTruthy();
    // The final answer: no "codex" label, bulleted like every other message.
    expect(screen.queryByText("codex")).toBeNull();
    expect(screen.getByText("Done.")).toBeTruthy();
    expect(screen.getAllByText("•")).toHaveLength(2); // the edit's bullet and the answer's
    // Codex prints no turn-end line of its own: plain meta, no glyph.
    expect(screen.getByText("Worked for 12s")).toBeTruthy();
    expect(screen.queryByText("✻", { includeHiddenElements: true })).toBeNull();
  });

  it("draws a Claude Edit as Update(path) with its result line and a numbered diff", async () => {
    const hunk = { oldStart: 1, oldLines: 5, newStart: 1, newLines: 5, lines: [" def greet(name):", '-    return "hello " + name', '+    return f"hello {name}"', " ", " def farewell(name):"] };
    await draw("claude", [
      ev(1, { kind: "tool", role: "assistant", text: "app.py", itemId: "e1", tool: { name: "Edit", status: "running", input: { file_path: "app.py" } } }),
      ev(2, { kind: "tool", role: "tool", text: "", parentId: "e1", tool: { name: "result", status: "done", result: { filePath: "app.py", structuredPatch: [hunk] } } }),
    ]);
    expect(screen.getByText("Update(app.py)")).toBeTruthy();
    expect(screen.getByText("Added 1 line, removed 1 line")).toBeTruthy();
    expect(screen.getByText('-    return "hello " + name', { normalizer: (t) => t })).toBeTruthy();
    expect(screen.getByText('+    return f"hello {name}"', { normalizer: (t) => t })).toBeTruthy();
    // A removed line under its old number, every other line under its new one: 1, 2-, 2+, 3, 4.
    const numbers = ["1 ", "2 ", "3 ", "4 "].flatMap((n) => screen.queryAllByText(n, { normalizer: (t) => t, includeHiddenElements: true }));
    expect(numbers.map((n) => n.props.children)).toEqual(["1 ", "2 ", "2 ", "3 ", "4 "]);
    // Decorative: VoiceOver skips the numbers.
    expect(numbers.every((n) => n.props.accessibilityElementsHidden === true)).toBe(true);
  });

  it("says how much a Write wrote, and opens a diff twelve lines deep", async () => {
    await draw("claude", [
      ev(1, { kind: "tool", role: "assistant", text: "notes.md", itemId: "w1", tool: { name: "Write", status: "running", input: { file_path: "notes.md", contentLines: 3 } } }),
      ev(2, { kind: "tool", role: "tool", text: "", parentId: "w1", tool: { name: "result", status: "done", result: { filePath: "notes.md", structuredPatch: [] } } }),
      ev(3, { kind: "tool", role: "assistant", text: "big.txt", itemId: "e2", tool: { name: "Edit", status: "running", input: {} } }),
      ev(4, { kind: "tool", role: "tool", text: "", parentId: "e2", tool: { name: "result", status: "done",
        result: { structuredPatch: [{ oldStart: 1, newStart: 1, lines: Array.from({ length: 15 }, (_, i) => `+L${i + 1}`) }] } } }),
    ]);
    expect(screen.getByText("Write(notes.md)")).toBeTruthy();
    expect(screen.getByText("Wrote 3 lines to notes.md")).toBeTruthy();
    expect(screen.getByText("+L12")).toBeTruthy();
    expect(screen.queryByText("+L13")).toBeNull();
    await fireEvent.press(screen.getByText("… +3 lines"));
    expect(screen.getByText("+L15")).toBeTruthy();
  });

  it("folds a Claude run into one unbulleted line in its own words that opens onto the calls", async () => {
    const rows = groupRows(buildRows([
      ev(1, { kind: "tool", role: "assistant", text: "a.ts", itemId: "r1", tool: { name: "Read", status: "done", input: {} } }),
      ev(2, { kind: "tool", role: "assistant", text: "npm test", itemId: "b1", tool: { name: "Bash", status: "done", input: { command: "npm test" } } }),
    ]), false);
    await render(<>{rows.map((row) => <TranscriptRowView key={row.seq} provider="claude" row={row} name={name} />)}</>);
    const group = screen.getByLabelText("Read 1 file, ran 1 shell command");
    expect(screen.queryByText("⏺", { includeHiddenElements: true })).toBeNull();
    await fireEvent.press(group);
    expect(screen.getByText("Read(a.ts)")).toBeTruthy();
    expect(screen.getByText("Bash(npm test)")).toBeTruthy();
  });

  it("draws Codex's Explored with a line per call, and a Ran row showing the end of its output", async () => {
    const run = (seq: number, command: string, parsed: unknown, output = "") =>
      ev(seq, { kind: "tool", role: "assistant", text: "", itemId: `t${seq}`,
        tool: { name: "CommandExecution", status: "done", input: { command, parsed: [parsed] }, result: { exitCode: 0, output }, lines: output ? output.trim().split("\n").length : 0 } });
    const rows = groupRows(buildRows([
      run(1, "sed -n '1,200p' app.py", { type: "read", name: "app.py", path: "app.py" }),
      run(2, "ls -la", { type: "list_files", path: null }),
      run(3, "seq 1 5", { type: "unknown" }, "C1\nC2\nC3\nC4\nC5\n"),
    ]), false);
    await render(<>{rows.map((row) => <TranscriptRowView key={row.seq} provider="codex" row={row} name={name} />)}</>);
    const group = screen.getByLabelText("Explored");
    expect(screen.getByText("Read app.py")).toBeTruthy();
    expect(screen.getByText("List ls -la")).toBeTruthy();
    // Codex's corner glyph on the first child line only; the ran row's preview carries the other.
    expect(screen.getAllByText("└", { includeHiddenElements: true })).toHaveLength(2);
    expect(screen.getByText("Ran seq 1 5")).toBeTruthy();
    expect(screen.getByText(/^C3\s+C4\s+C5$/)).toBeTruthy();
    expect(screen.queryByText(/C1/)).toBeNull();
    await fireEvent.press(group);
    expect(screen.getByText("Ran sed -n '1,200p' app.py")).toBeTruthy();
  });

  it("ends a Claude turn with its glyph line", async () => {
    await draw("claude", [
      ev(1, { kind: "text", role: "assistant", text: "Done.", itemId: "m1" }),
      ev(2, { kind: "lifecycle", role: "system", text: "25000", lifecycle: "turn_end" }),
    ]);
    expect(screen.getByText("✻", { includeHiddenElements: true })).toBeTruthy();
    expect(screen.getByText("Worked for 25s")).toBeTruthy();
  });

  it("shows a working line with elapsed seconds", async () => {
    await render(<WorkingRow provider="claude" since={new Date(Date.now() - 4000).toISOString()} />);
    expect(screen.getByLabelText("Working")).toBeTruthy();
    expect(screen.getByText(/Working…/)).toBeTruthy();
  });
});

describe("PendingPromptRow", () => {
  it("draws the unsent follow-up greyed with one status chip", async () => {
    await render(<PendingPromptRow provider="claude" text="run the tests" ownerName="You"
      chip={{ label: "Queued on your Mac", tone: "pending", detail: "Admission is not completed work." }} />);
    expect(screen.getByLabelText("Pending follow-up")).toBeTruthy();
    expect(screen.getByText("run the tests")).toBeTruthy();
    expect(screen.getByLabelText("Queued on your Mac. Admission is not completed work.")).toBeTruthy();
  });
});
