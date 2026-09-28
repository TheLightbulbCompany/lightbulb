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

  it("collapses thinking to one line and folds a run of tools into a summary that expands", async () => {
    const rows = groupRows(buildRows([
      ev(1, { kind: "thinking", role: "assistant", text: "SECRET_PLAN", ts: "2026-09-24T10:00:00.000Z" }),
      ev(2, { kind: "tool", role: "assistant", text: "a.ts", itemId: "a", ts: "2026-09-24T10:00:05.000Z", tool: { name: "Edit", status: "done", input: {} } }),
      ev(3, { kind: "tool", role: "assistant", text: "npm test", itemId: "b", tool: { name: "Bash", status: "done", input: {} } }),
    ]), false);
    await render(<>{rows.map((row) => <TranscriptRowView key={row.seq} provider="claude" row={row} name={name} />)}</>);
    expect(screen.queryByText(/SECRET_PLAN/)).toBeNull();
    await fireEvent.press(screen.getByLabelText("Thought for 5s"));
    expect(screen.getByText(/SECRET_PLAN/)).toBeTruthy();
    const group = screen.getByLabelText("Ran 2 tools · 1 edit");
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
    expect(screen.getByText("Edited a.ts")).toBeTruthy();
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
