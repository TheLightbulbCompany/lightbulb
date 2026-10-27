import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { LocalSessionEvent } from "@lightbulb/shared";
import { SessionOverview } from "../SessionOverview";

const ev = (seq: number, e: Omit<LocalSessionEvent, "seq" | "truncated">): LocalSessionEvent => ({ seq, truncated: false, ...e });

describe("SessionOverview", () => {
  it("draws nothing for a session with no plan and no edits", () => {
    const { container } = render(<SessionOverview events={[ev(1, { kind: "text", role: "assistant", text: "hi" })]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("pins the plan as a checklist and totals the changed files", () => {
    render(<SessionOverview events={[
      ev(1, { kind: "tool", role: "assistant", text: "Update Todos", itemId: "t1", tool: { name: "TodoWrite", status: "running", input: { todos: [
        { content: "Map it", status: "completed" }, { content: "Fix it", status: "in_progress" },
      ] } } }),
      ev(2, { kind: "tool", role: "assistant", text: "src/a.ts", itemId: "e1", tool: { name: "Edit", status: "running", input: { file_path: "src/a.ts" } } }),
      ev(3, { kind: "tool", role: "tool", text: "ok", parentId: "e1", tool: { name: "result", status: "done", result: { filePath: "a.ts", structuredPatch: [{ lines: ["-old", "+new", "+more"] }] } } }),
    ]} />);
    expect(screen.getByText("Plan · 1 of 2 done")).toBeInTheDocument();
    expect(screen.getByText("In progress:")).toBeInTheDocument();
    expect(screen.getByText("Fix it", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("What changed · 1 file", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("src/a.ts")).toBeInTheDocument();
    expect(screen.getByText("+new")).toBeInTheDocument();
  });
});

it("offers the reader's action on the changes inside What changed", () => {
  const events = [
    ev(1, { kind: "tool", role: "assistant", text: "src/a.ts", itemId: "e1", tool: { name: "Edit", status: "running", input: { file_path: "src/a.ts" } } }),
    ev(2, { kind: "tool", role: "tool", text: "ok", parentId: "e1", tool: { name: "result", status: "done", result: { filePath: "a.ts", structuredPatch: [{ lines: ["-old", "+new"] }] } } }),
  ];
  render(<SessionOverview events={events} changesAction={<button type="button">Commit &amp; open PR</button>} />);
  expect(screen.getByRole("button", { name: "Commit & open PR" })).toBeInTheDocument();
});

it("shows git's count from the Mac when no edit event names a file, and says it is from git", () => {
  render(<SessionOverview events={[ev(1, { kind: "text", role: "assistant", text: "hi" })]} git={{ files: 3, additions: 12, deletions: 4 }} />);
  expect(screen.getByText("What changed · 3 files", { exact: false })).toBeInTheDocument();
  expect(screen.getByText("+12")).toBeInTheDocument();
  expect(screen.getByText("−4")).toBeInTheDocument();
  expect(screen.getByText("from git, since the session started")).toBeInTheDocument();
});
