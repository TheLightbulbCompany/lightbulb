import { describe, expect, it } from "vitest";

import { sessionChanges, sessionPlan } from "./localSessionOverview";
import type { LocalSessionEvent } from "./types";

const ev = (seq: number, e: Omit<LocalSessionEvent, "seq" | "truncated">): LocalSessionEvent => ({ seq, truncated: false, ...e });
const call = (seq: number, name: string, input: unknown, text = "", itemId = `t${seq}`) =>
  ev(seq, { kind: "tool", role: "assistant", text, itemId, tool: { name, status: "running", input } });
const result = (seq: number, parentId: string, text: string, payload: unknown, status: "done" | "error" = "done") =>
  ev(seq, { kind: "tool", role: "tool", text, parentId, tool: { name: "result", status, result: payload } });

describe("sessionPlan", () => {
  it("is null when the session never planned", () => {
    expect(sessionPlan([call(1, "Bash", { command: "ls" })])).toBeNull();
  });

  it("takes the latest whole list from TodoWrite and Codex update_plan", () => {
    expect(sessionPlan([
      call(1, "TodoWrite", { todos: [{ content: "old", status: "pending" }] }),
      call(2, "TodoWrite", { todos: [{ content: "Map it", status: "completed" }, { content: "Fix it", status: "in_progress" }, { status: "pending" }] }),
    ])).toEqual([{ text: "Map it", status: "completed" }, { text: "Fix it", status: "in_progress" }]);
    expect(sessionPlan([call(1, "update_plan", { plan: [{ step: "Ship", status: "weird" }] })])).toEqual([{ text: "Ship", status: "pending" }]);
  });

  it("builds Claude's task list from creates and updates, by the id the result reports", () => {
    expect(sessionPlan([
      call(1, "TaskCreate", { subject: "Map it" }, "Map it", "a"),
      result(2, "a", "Task #7 created successfully: Map it", { content: "Task #7 created successfully: Map it" }),
      call(3, "TaskCreate", { subject: "Drop me" }, "Drop me", "b"),
      result(4, "b", "Task #8 created successfully: Drop me", {}),
      call(5, "TaskUpdate", { taskId: "7", status: "in_progress" }),
      call(6, "TaskUpdate", { taskId: "8", status: "deleted" }),
      // Created before the loaded window: named by id until a subject arrives.
      call(7, "TaskUpdate", { taskId: "3", status: "completed" }),
    ])).toEqual([{ text: "Map it", status: "in_progress" }, { text: "Task #3", status: "completed" }]);
  });
});

describe("sessionChanges", () => {
  it("sums Claude edits per file, sizes new files from contentLines, and skips failures", () => {
    const changes = sessionChanges([
      call(1, "Edit", { file_path: "src/a.ts" }, "src/a.ts", "e1"),
      result(2, "e1", "ok", { filePath: "a.ts", structuredPatch: [{ lines: [" keep", "-old", "+new", "+more"] }] }),
      call(3, "Edit", { file_path: "src/a.ts" }, "src/a.ts", "e2"),
      result(4, "e2", "ok", { filePath: "a.ts", structuredPatch: [{ lines: ["-gone"] }] }),
      call(5, "Write", { file_path: "src/new.ts", contentLines: 12 }, "src/new.ts", "w1"),
      result(6, "w1", "ok", { filePath: "new.ts", structuredPatch: [] }),
      call(7, "Edit", { file_path: "src/bad.ts" }, "src/bad.ts", "e3"),
      result(8, "e3", "no", { filePath: "bad.ts", structuredPatch: [{ lines: ["+x"] }] }, "error"),
      call(9, "Read", { file_path: "src/a.ts" }, "src/a.ts", "r1"),
      result(10, "r1", "…", { numLines: 3 }),
    ]);
    expect(changes).toEqual([
      { path: "src/a.ts", added: 2, removed: 2, diff: [" keep", "-old", "+new", "+more", "-gone"] },
      { path: "src/new.ts", added: 12, removed: 0, diff: [] },
    ]);
  });

  it("reads Codex FileChange diffs, added and deleted files", () => {
    const changes = sessionChanges([ev(1, { kind: "tool", role: "assistant", text: "a.md, b.md, c.md", itemId: "f1", tool: { name: "FileChange", status: "done", result: {
      "a.md": { type: "update", diff: "@@ -1 +1 @@\n-a\n+b" },
      "b.md": { type: "add", content: "one\ntwo\n" },
      "c.md": { type: "delete", content: "gone" },
    } } })]);
    expect(changes.map(({ path, added, removed }) => [path, added, removed])).toEqual([["a.md", 1, 1], ["b.md", 2, 0], ["c.md", 0, 1]]);
  });
});
