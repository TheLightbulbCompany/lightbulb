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

type Found = { tasks: TaskSummary[]; files: CreationOutput[] };
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
      .then((text) => text.toLowerCase(), () => { heads.delete(key); return ""; });
    heads.set(key, head);
  }
  return head;
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
        Promise.all(messaging ? docs.map((o) => docHead(messaging, o).then((text) => (text.includes(needle) ? o : null))) : []),
      ]).then(([found, named, inText]) => {
        const seen = new Set(named.map((o) => `${o.taskId}:${o.id}`));
        const files = [...named, ...inText.filter((o): o is CreationOutput => !!o && !seen.has(`${o.taskId}:${o.id}`))];
        if (!abort.signal.aborted) setResult({ term, found: { tasks: found, files: files.slice(0, MAX) } });
      });
    }, DEBOUNCE_MS);
    return () => { abort.abort(); clearTimeout(timer); };
  }, [active, term, tasks, messaging, docs]);

  return active && result.term === term ? result.found : NONE;
}
