"use client";

// One opener for what a task made (UX slice 5), shared by Creations, Made here, Files and the chips on a task:
// an app opens in the split view, a doc or file in the file viewer, a link or PR in a new tab, and mail or a
// capability on its task (the transcript shows what was sent).

import { createContext } from "react";
import type { OutputKind, TaskOutput } from "@lightbulb/shared";
import { openPanel } from "@/components/workspace/workspaceController";
import { buildPanelUrl } from "@/components/workspace/urlChannel";
import { readableName } from "./docText";

export type Openable = Pick<TaskOutput, "kind" | "title" | "ref"> & { taskId: string };

/** The panel an output opens in, or the outside address of a link or PR. */
function target(o: Openable): { panel: string; params: Record<string, string> } | { url: string } | null {
  const ref = o.ref ?? {};
  switch (o.kind) {
    case "app": return ref.appId ? { panel: "canvas", params: { app: ref.appId } } : null;
    case "document":
    case "file": return ref.roomId && ref.attachmentId ? {
      panel: "artifact",
      params: { src: "attachment", room: ref.roomId, id: ref.attachmentId, ...(ref.fileName ? { name: ref.fileName } : {}), ...(ref.contentType ? { type: ref.contentType } : {}) },
    } : null;
    case "link":
    case "pr": return ref.url && /^https?:\/\//.test(ref.url) ? { url: ref.url } : null;
    default: return null;
  }
}

const taskTarget = (taskId: string) => ({ panel: "task-detail", params: { taskId } });

/** Open it. `openTask` opens the task where the caller lives (a project's pane); else the shell's task pane. `back`:
 *  where Escape and "‹" return from the opened file (leavePane), when it was opened from inside the pane. */
export function openOutput(o: Openable, openTask?: (taskId: string) => void, back?: string): void {
  const t = target(o);
  if (t && "url" in t) { window.open(t.url, "_blank", "noopener,noreferrer"); return; }
  // Only a file in the pane has a way back; an app opens as the place.
  if (t) { openPanel(t.panel, t.params, { pushUrl: true, ...(back && t.panel === "artifact" ? { oneShots: { back } } : {}) }); return; }
  if (openTask) openTask(o.taskId);
  else openPanel("task-detail", { taskId: o.taskId }, { pushUrl: true });
}

/** Where a file opened inside it goes back to ("task:<id>"): set by an open task, read by its message attachments, so
 *  the doc card in a task's Progress opens with "‹ Task" and Escape returns to the task (W12-5). */
export const OutputBackContext = createContext<string | undefined>(undefined);

/** A link to what Open shows: the outside address for a link, else this app's URL for the same pane. */
export function outputLink(o: Openable): string {
  const t = target(o) ?? taskTarget(o.taskId);
  return "url" in t ? t.url : new URL(buildPanelUrl(t.panel, t.params), window.location.origin).toString();
}

/** What a person calls it: a doc or file by a readable name ("Autumn specials menu blurbs"), never its file name. */
export const outputName = (o: Pick<TaskOutput, "kind" | "title">): string =>
  o.kind === "document" || o.kind === "file" ? readableName(o.title) : o.title;

/** The word for a kind ("App · walk-7 · 2h"). */
export const KIND_WORD: Record<OutputKind, string> = {
  app: "App", document: "Doc", file: "File", mail: "Mail", link: "Link", pr: "Link", capability: "Skill",
};

/** The filters: Creations' segments and the Files type select. Links is one read for link and PR. */
export const KIND_FILTERS: readonly { id: string; label: string; kinds?: readonly OutputKind[] }[] = [
  { id: "all", label: "All" },
  { id: "apps", label: "Apps", kinds: ["app"] },
  { id: "docs", label: "Docs", kinds: ["document"] },
  { id: "files", label: "Files", kinds: ["file"] },
  { id: "mail", label: "Mail", kinds: ["mail"] },
  { id: "links", label: "Links", kinds: ["link", "pr"] },
];

/** "notes.md" → "MD"; nothing when the name has no extension. */
export function extensionOf(o: Pick<TaskOutput, "title" | "ref">): string {
  const name = o.ref?.fileName || o.title;
  const dot = name.lastIndexOf(".");
  return dot > 0 && dot >= name.length - 6 ? name.slice(dot + 1).toUpperCase() : "";
}

/** The host of a link or PR ("github.com"). */
export function hostOf(o: Pick<TaskOutput, "ref">): string {
  try { return new URL(o.ref?.url ?? "").host.replace(/^www\./, ""); } catch { return ""; }
}
