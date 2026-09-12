import { render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { LocalSessionEvent } from "@lightbulb/shared";
import { buildRows, NativeSessionTranscript } from "../NativeSessionTranscript";

const people = [{ kind: "human" as const, id: "user_a", displayName: "Aden Kim" }, { kind: "agent" as const, id: "friday", displayName: "Friday" }];
const ev = (seq: number, e: Omit<LocalSessionEvent, "seq" | "truncated"> & { truncated?: boolean }): LocalSessionEvent => ({ seq, truncated: false, ...e });

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
});

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
  });
});
