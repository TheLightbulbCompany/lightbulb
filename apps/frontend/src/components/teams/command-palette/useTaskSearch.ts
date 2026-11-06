"use client";

// ⌘K's Tasks and Files groups (W12-3): tasks by title and what they made by name, from `GET /tasks?q=` and
// `GET /tasks/outputs?q=`. The server keeps only what the viewer can open (`tasks_access.can_view`).

import { useEffect, useState } from "react";
import type { CreationOutput, TaskSummary } from "@lightbulb/shared";
import { useLiveEvents } from "@/hooks/useLiveEvents";

const DEBOUNCE_MS = 250;
const MAX = 6;

type Found = { tasks: TaskSummary[]; files: CreationOutput[] };
const NONE: Found = { tasks: [], files: [] };

export function useTaskSearch(query: string, enabled: boolean): Found {
  const { tasks } = useLiveEvents();
  const term = query.trim();
  const active = enabled && term.length >= 2;
  const [result, setResult] = useState<{ term: string; found: Found }>({ term: "", found: NONE });

  useEffect(() => {
    if (!active) return;
    let live = true;
    const timer = setTimeout(() => {
      // A failed half shows nothing for that group; the rest of the palette still answers.
      void Promise.all([
        tasks.list({ kind: "workspace" }, null, term).then((r) => r.tasks, () => []),
        tasks.outputs({ q: term, limit: 10 }).then((r) => r.outputs, () => []),
      ]).then(([found, files]) => {
        if (live) setResult({ term, found: { tasks: found.slice(0, MAX), files: files.slice(0, MAX) } });
      });
    }, DEBOUNCE_MS);
    return () => { live = false; clearTimeout(timer); };
  }, [active, term, tasks]);

  return active && result.term === term ? result.found : NONE;
}
