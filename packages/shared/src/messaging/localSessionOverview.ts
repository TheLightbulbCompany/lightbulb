import { diffLines } from "./localSessionTranscript";
import type { LocalSessionEvent } from "./types";

/**
 * What a shared session is doing at a glance: its current plan and the files
 * it changed. Pure, like the transcript model, and computed from the same
 * loaded window of events -- a session's first plan or edits may sit on an
 * older page until the reader loads it.
 */
export type PlanStatus = "pending" | "in_progress" | "completed";
export interface PlanStep { text: string; status: PlanStatus }
export interface FileChange { path: string; added: number; removed: number; diff: string[] }

const record = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" ? (value as Record<string, unknown>) : {};
const str = (value: unknown) => (typeof value === "string" && value ? value : undefined);
const status = (value: unknown): PlanStatus =>
  value === "completed" || value === "in_progress" ? value : "pending";

/** Tool results keyed by the call they answer (Claude writes them as separate events). */
function resultsByCall(events: readonly LocalSessionEvent[]) {
  const results = new Map<string, LocalSessionEvent>();
  for (const e of events) if (e.kind === "tool" && e.role === "tool" && e.parentId) results.set(e.parentId, e);
  return results;
}

/**
 * The latest checklist, or null when the session never wrote one. Claude's
 * TodoWrite and Codex's update_plan send the whole list each time; Claude's
 * task list (TaskCreate / TaskUpdate) edits it one task at a time, and a task
 * is known by the id its creation result reports ("Task #13 created …").
 */
export function sessionPlan(events: readonly LocalSessionEvent[]): PlanStep[] | null {
  const results = resultsByCall(events);
  const whole = (items: unknown, text: string) => new Map((Array.isArray(items) ? items : []).flatMap((item, i) => {
    const step = record(item);
    const label = str(step[text]);
    return label ? [[String(i), { text: label, status: status(step.status) }] as const] : [];
  }));
  let steps: Map<string, PlanStep> | null = null;
  for (const e of events) {
    if (e.kind !== "tool" || e.role === "tool" || !e.tool) continue;
    const input = record(e.tool.input);
    if (e.tool.name === "TodoWrite") steps = whole(input.todos, "content");
    else if (e.tool.name === "update_plan") steps = whole(input.plan, "step");
    else if (e.tool.name === "TaskCreate") {
      const subject = str(input.subject) ?? e.text;
      if (!subject) continue;
      const created = results.get(e.itemId ?? "")?.text.match(/Task #(\w+)/)?.[1];
      steps ??= new Map();
      steps.set(created ?? `call:${e.itemId ?? e.seq}`, { text: subject, status: "pending" });
    } else if (e.tool.name === "TaskUpdate") {
      const id = str(input.taskId);
      if (!id) continue;
      steps ??= new Map();
      if (input.status === "deleted") { steps.delete(id); continue; }
      const known = steps.get(id);
      steps.set(id, {
        text: str(input.subject) ?? known?.text ?? `Task #${id}`,
        status: input.status === undefined ? known?.status ?? "pending" : status(input.status),
      });
    }
  }
  return steps ? [...steps.values()] : null;
}

const counts = (diff: readonly string[]) => ({
  added: diff.filter(l => l.startsWith("+") && !l.startsWith("+++")).length,
  removed: diff.filter(l => l.startsWith("-") && !l.startsWith("---")).length,
});

/**
 * Every file the loaded events changed, with +/− line counts and the diff
 * lines, in first-touched order. Claude's Edit/Write/MultiEdit results carry
 * a structuredPatch (a new file's is empty, so its size comes from the call's
 * contentLines); a Codex FileChange carries a per-file diff or, for an added
 * or deleted file, its content. Failed edits changed nothing and are skipped.
 */
export function sessionChanges(events: readonly LocalSessionEvent[]): FileChange[] {
  const results = resultsByCall(events);
  const files = new Map<string, FileChange>();
  const add = (path: string, diff: string[], created = 0) => {
    const file = files.get(path) ?? { path, added: 0, removed: 0, diff: [] };
    const n = counts(diff);
    file.added += n.added + created;
    file.removed += n.removed;
    file.diff.push(...diff);
    files.set(path, file);
  };
  for (const e of events) {
    if (e.kind !== "tool" || e.role === "tool" || !e.tool) continue;
    if (e.tool.name === "FileChange") {
      if (e.tool.status === "error") continue;
      for (const [path, change] of Object.entries(record(e.tool.result))) {
        const c = record(change);
        const content = str(c.content);
        const mark = c.type === "delete" ? "-" : "+";
        if (str(c.diff)) add(path, diffLines({ [path]: c }));
        else if (content) add(path, content.replace(/\n$/, "").split("\n").map(line => `${mark}${line}`));
      }
      continue;
    }
    const result = results.get(e.itemId ?? "");
    const payload = record(result?.tool?.result);
    if (!result || result.tool?.status === "error" || !Array.isArray(payload.structuredPatch)) continue;
    const input = record(e.tool.input);
    const path = str(input.file_path) ?? str(payload.filePath) ?? e.text;
    const created = payload.structuredPatch.length === 0 && typeof input.contentLines === "number" ? input.contentLines : 0;
    add(path, diffLines(payload), created);
  }
  return [...files.values()];
}
