"use client";

// ⌘K's Tasks and Files groups (W12-3): tasks by title and what they made by name, from `GET /tasks?q=` and
// `GET /tasks/outputs?q=`. The server keeps only what the viewer can open (`tasks_access.can_view`). Each keystroke
// aborts the previous request, so only the latest query reaches the server's task walk.
// Files also match a doc's words (W15-10): the newest text docs the viewer can list, each read once per tab through
// the same ranged attachment read the doc tiles use (`?head=`), matched here.

import { useEffect, useState } from "react";
import type { CreationOutput, TaskSummary } from "@lightbulb/shared";
import { isTextDoc } from "@/components/creations/OutputThumb";
import { blobText } from "@/components/media/readBlob";
import { useLiveEvents } from "@/hooks/useLiveEvents";

const DEBOUNCE_MS = 250;
const MAX = 6;
/** Shorter terms pull in unrelated words (W15-10). */
export const MIN_TERM = 3;
// ponytail: the newest 24 docs' first 8 KB; a server-side text index when docs outgrow that.
const DOC_TASKS = 24;
const DOC_HEAD = 8192;

/** A file hit with the words around the match, when they are known (a doc's head, a short result's text). */
export type FoundFile = CreationOutput & { snippet?: string };
type Found = { tasks: TaskSummary[]; files: FoundFile[] };
type Messaging = ReturnType<typeof useLiveEvents>["messaging"];
const NONE: Found = { tasks: [], files: [] };

/** Attachments never change, so a doc's head is read once per tab. A failed read is forgotten and tried again. */
const heads = new Map<string, Promise<string>>();
function docHead(messaging: Messaging, o: CreationOutput): Promise<string> {
  const { roomId, attachmentId } = o.ref ?? {};
  if (!roomId || !attachmentId || !isTextDoc(o)) return Promise.resolve("");
  const key = `${roomId}:${attachmentId}`;
  let head = heads.get(key);
  if (!head) {
    head = messaging.downloadAttachment(roomId, attachmentId, { head: DOC_HEAD }).then(blobText)
      .then((text) => text, () => { heads.delete(key); return ""; });
    heads.set(key, head);
  }
  return head;
}

/** The words around `needle` in a doc (W16-11: a doc row showed only its slug): one line, markdown marks off, about
 *  90 characters, "…" where it was cut. `lower` is the text lowercased (what was matched); `text` the original, when
 *  there is one. No match: its opening words. */
export function snippetOf(lower: string, needle: string, text = lower): string {
  const at = Math.max(0, lower.indexOf(needle));
  const start = Math.max(0, at - 30);
  const flat = (t: string) => t.replace(/[#*_`>[\]()]+/g, "").replace(/\s+/g, " ").trim();
  const cut = flat(text.slice(start, start + 120)).replace(start > 0 ? /^\S*\s/ : /^/, "");  // never half a word
  return `${start > 0 ? "…" : ""}${cut.length > 90 ? `${cut.slice(0, 90).replace(/\s+\S*$/, "")}…` : cut}`;
}

export function useTaskSearch(query: string, enabled: boolean): Found {
  const { tasks, messaging } = useLiveEvents();
  const term = query.trim();
  const active = enabled && term.length >= MIN_TERM;
  const [result, setResult] = useState<{ term: string; found: Found }>({ term: "", found: NONE });
  // The docs to read, once per palette open.
  const [docs, setDocs] = useState<CreationOutput[]>([]);
  useEffect(() => {
    if (!enabled || !messaging) return;
    const abort = new AbortController();
    tasks.outputs({ kind: "document", limit: DOC_TASKS }, abort.signal)
      .then((r) => { if (!abort.signal.aborted) setDocs(r.outputs.filter(isTextDoc)); }, () => undefined);
    return () => abort.abort();
  }, [enabled, tasks, messaging]);

  useEffect(() => {
    if (!active) return;
    const abort = new AbortController();
    const timer = setTimeout(() => {
      const needle = term.toLowerCase();
      // A failed half shows nothing for that group; the rest of the palette still answers.
      void Promise.all([
        tasks.searchTasks(term, MAX, abort.signal).then((r) => r.tasks, () => []),
        tasks.outputs({ q: term, limit: MAX }, abort.signal).then((r): CreationOutput[] => r.outputs, () => []),
        Promise.all(messaging ? docs.map((o) => docHead(messaging, o).then((text): FoundFile | null => (text.toLowerCase().includes(needle) ? { ...o, snippet: snippetOf(text.toLowerCase(), needle, text) } : null))) : []),
      ]).then(([found, named, inText]) => {
        const seen = new Set(named.map((o) => `${o.taskId}:${o.id}`));
        const files: FoundFile[] = [
          ...named.map((o): FoundFile => (o.ref?.text ? { ...o, snippet: snippetOf(o.ref.text.toLowerCase(), needle, o.ref.text) } : o)),
          ...inText.filter((o): o is FoundFile => !!o && !seen.has(`${o.taskId}:${o.id}`)),
        ];
        if (!abort.signal.aborted) setResult({ term, found: { tasks: found, files: files.slice(0, MAX) } });
      });
    }, DEBOUNCE_MS);
    return () => { abort.abort(); clearTimeout(timer); };
  }, [active, term, tasks, messaging, docs]);

  return active && result.term === term ? result.found : NONE;
}
