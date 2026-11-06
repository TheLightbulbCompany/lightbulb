"use client";

// "Made here" (super-agent, UX slice 5): the newest four things this project's tasks made, at the top of its Tasks
// pane, each with its readable name under it (a doc's face is its title and first lines, OutputThumb). Hidden until the project has made something; "All files ›" switches the pane to Files.

import "./creations.css";
import type { ReactElement } from "react";
import { ChevronRight } from "lucide-react";
import { useApps } from "@/hooks/useApps";
import { openOutput, outputName } from "./openOutput";
import { OutputThumb } from "./OutputThumb";
import { useCreations } from "./useCreations";

export function MadeHere({ roomId, onOpenTask, onAllFiles }: {
  roomId: string;
  onOpenTask: (taskId: string) => void;
  onAllFiles: () => void;
}): ReactElement | null {
  const { outputs } = useCreations({ roomId, limit: 4 });
  const { apps } = useApps();
  if (!outputs.length) return null;
  return <section className="made-here" aria-labelledby={`made-here-${roomId}`}>
    <div className="made-here__head">
      <h4 id={`made-here-${roomId}`} className="creations__label">Made here</h4>
      <button type="button" className="made-here__all" onClick={onAllFiles}>All files<ChevronRight size={14} aria-hidden="true" /></button>
    </div>
    <ul className="made-here__row">
      {outputs.slice(0, 4).map((o) => <li key={`${o.taskId}:${o.id}`}>
        <button type="button" className="made-here__tile" title={o.title} aria-label={`Open ${outputName(o)}`} onClick={() => openOutput(o, onOpenTask, "tasks")}>
          <OutputThumb output={o} size="mini" app={o.kind === "app" ? apps.find((a) => a.app_id === o.ref?.appId) : undefined} />
          <span className="made-here__name">{outputName(o)}</span>
        </button>
      </li>)}
    </ul>
  </section>;
}
