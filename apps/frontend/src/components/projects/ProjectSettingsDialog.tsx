"use client";

// Project settings (UX slice 6): a modal over the project, so Esc puts you back exactly where you were (pane, scroll,
// draft). General (name and icon, Pause, Archive, Delete) · Memory (the Instructions every task here reads) · People
// (the Members content, handed in). Each field saves on change or blur and says "Saved" beside itself for 2 s.
// ponytail: "What Lightbulb remembers here" stays hidden: memory-engine facts carry no project tag yet (08 §6c spike).

import { useEffect, useId, useRef, useState, type MutableRefObject, type ReactElement, type ReactNode } from "react";
import { useOrganization } from "@clerk/nextjs";
import { X } from "lucide-react";
import { ROOM_TOPIC_MAX_CHARS, type PatchRoomRequest, type RoomSummary } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { notifyApiError } from "@/lib/notify";
import { ProjectIconPicker } from "./ProjectTile";

export type SettingsSection = "general" | "memory" | "people";
/** Where ⋯ › Settings was chosen outside the project (focus goes back there on close). */
export type SettingsFrom = "sidebar" | "home";
const SECTIONS: { id: SettingsSection; label: string }[] = [
  { id: "general", label: "General" }, { id: "memory", label: "Memory" }, { id: "people", label: "People" },
];
const SAVED_MS = 2000;

/** "Saved" for 2 s after `token` changes (0 = never saved). */
function Saved({ token }: { token: number }): ReactElement | null {
  const [shown, setShown] = useState(0);
  useEffect(() => {
    if (!token) return;
    const t = setTimeout(() => setShown(token), 0);
    const off = setTimeout(() => setShown(0), SAVED_MS);
    return () => { clearTimeout(t); clearTimeout(off); };
  }, [token]);
  return shown ? <span role="status" className="project-saved">Saved</span> : null;
}

export interface ProjectSettingsProps {
  room: RoomSummary;
  roomName: string;
  section: SettingsSection | null;
  onSection: (section: SettingsSection | null) => void;
  /** The room's owner or admin: name, icon, Instructions, Archive. */
  canEdit: boolean;
  actionable: boolean;
  onRename?: (name: string) => Promise<void>;
  onSetTopic?: (topic: string | null) => Promise<void>;
  onPatch?: (request: PatchRoomRequest) => Promise<void>;
  /** Open the header's own confirm dialogs (this modal closes first). */
  onArchive?: () => void;
  onDelete?: () => void;
  people: ReactNode;
  /** Where focus goes once it closes (the trigger it was opened from). */
  onCloseAutoFocus?: (event: Event) => void;
}

type Flush = MutableRefObject<(() => void) | null>;

export function ProjectSettingsDialog(props: ProjectSettingsProps): ReactElement {
  const { section, onSection, roomName, onCloseAutoFocus } = props;
  // Esc or ✕ while a field still has focus unmounts it before its blur save runs: save what is unsaved first.
  const flush: Flush = useRef(null);
  return <Dialog open={section !== null} onOpenChange={(open) => { if (!open) { flush.current?.(); onSection(null); } }}>
    <DialogContent className="project-settings" onCloseAutoFocus={onCloseAutoFocus}>
      <div className="project-settings__head">
        <DialogTitle asChild><h2 className="truncate">{roomName} · Settings</h2></DialogTitle>
        <DialogClose asChild><Button type="button" variant="ghost" size="icon-sm" aria-label="Close settings"><X aria-hidden="true" /></Button></DialogClose>
      </div>
      {section && <SettingsBody {...props} section={section} flush={flush} />}
    </DialogContent>
  </Dialog>;
}

function SettingsBody({ room, roomName, section, onSection, canEdit, actionable, onRename, onSetTopic, onPatch, onArchive, onDelete, people, flush }: ProjectSettingsProps & { section: SettingsSection; flush: Flush }): ReactElement {
  const id = useId();
  const { membership } = useOrganization();
  // The backend lets a room owner/admin pause, and a workspace admin too (pause alone).
  // Never #general or the channel shared with the Lightbulb team.
  const canPause = !!onPatch && (canEdit || membership?.role === "org:admin") && !room.isDefault && !room.shared;
  const [name, setName] = useState(roomName);
  const [topic, setTopic] = useState(room.topic ?? "");
  const [saved, setSaved] = useState<Record<string, number>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const save = async (key: string, action: () => Promise<void>, fallback: string) => {
    setBusy(true);
    setError(null);
    try { await action(); setSaved((s) => ({ ...s, [key]: (s[key] ?? 0) + 1 })); }
    catch (caught) { setError(caught instanceof Error && caught.message ? caught.message : fallback); }
    finally { setBusy(false); }
  };
  const editable = canEdit && actionable && !busy;
  const nameDirty = !!onRename && editable && !room.shared && !!name.trim() && name.trim() !== roomName;
  const topicDirty = !!onSetTopic && editable && topic.trim() !== (room.topic ?? "");
  // Closing with an unsaved field: save it after this unmounts; a failure is a toast, where the person still looks.
  useEffect(() => {
    flush.current = () => {
      if (nameDirty) onRename!(name.trim()).catch((e) => notifyApiError(e, "The name could not be saved."));
      if (topicDirty) onSetTopic!(topic.trim() || null).catch((e) => notifyApiError(e, "The instructions could not be saved."));
    };
    return () => { flush.current = null; };
  }, [flush, nameDirty, topicDirty, name, topic, onRename, onSetTopic]);
  const paused = !!room.pausedAt;

  const general = <>
    <h3>General</h3>
    <div className="project-row">
      <div className="project-row__text">
        <p className="project-row__label"><label htmlFor={`${id}-name`}>Name and icon</label></p>
        <p className="project-row__hint">How this project shows in the sidebar and on Home.</p>
      </div>
      <div className="project-row__control">
        <Saved token={saved.name ?? 0} />
        <div className="project-field">
          <ProjectIconPicker icon={room.icon} color={room.color} name={name} disabled={!editable || !onPatch}
            onChange={(next) => void save("name", () => onPatch!({ icon: next.icon ?? null, color: next.color ?? null }), "The icon could not be saved.")} />
          <input id={`${id}-name`} value={name} maxLength={200} disabled={!editable || !onRename || room.shared}
            onChange={(e) => setName(e.target.value)}
            onBlur={() => { if (nameDirty) void save("name", () => onRename!(name.trim()), "The name could not be saved."); }}
            onKeyDown={(e) => { if (e.key === "Enter") e.currentTarget.blur(); }} />
        </div>
      </div>
    </div>
    {canPause && <div className="project-row">
      <div className="project-row__text">
        <p className="project-row__label">{paused ? "Paused" : "Pause project"}</p>
        <p className="project-row__hint">Stops new tasks and schedules here. Running tasks finish. Undo any time.</p>
      </div>
      <div className="project-row__control">
        <Saved token={saved.pause ?? 0} />
        <Button type="button" size="sm" variant="outline" className="h-7" disabled={!actionable || busy}
          onClick={() => void save("pause", () => onPatch!({ paused: !paused }), paused ? "The project could not be resumed." : "The project could not be paused.")}>
          {paused ? "Resume" : "Pause"}</Button>
      </div>
    </div>}
    {onArchive && <div className="project-row">
      <div className="project-row__text">
        <p className="project-row__label">Archive project</p>
        <p className="project-row__hint">Hides it from the sidebar. Undo any time.</p>
      </div>
      <div className="project-row__control"><Button type="button" size="sm" variant="outline" className="h-7" disabled={!actionable} onClick={onArchive}>Archive</Button></div>
    </div>}
    {onDelete && <div className="project-row">
      <div className="project-row__text">
        <p className="project-row__label">Delete project</p>
        <p className="project-row__hint">Removes it and its chat for everyone. Can&apos;t be undone.</p>
      </div>
      <div className="project-row__control"><Button type="button" size="sm" variant="destructive" className="h-7" disabled={!actionable} onClick={onDelete}>Delete</Button></div>
    </div>}
  </>;

  const memory = <>
    <h3>Memory</h3>
    <div className="project-row">
      <div className="project-row__text grid gap-2">
        <div className="flex items-baseline gap-2">
          <p className="project-row__label mr-auto"><label htmlFor={`${id}-topic`}>Instructions</label></p>
          <Saved token={saved.topic ?? 0} />
        </div>
        <p id={`${id}-topic-hint`} className="project-row__hint">Lightbulb and the specialists read this before every task in this project.</p>
        <textarea id={`${id}-topic`} className="project-textarea" style={{ minHeight: 120 }} aria-describedby={`${id}-topic-hint`}
          value={topic} maxLength={ROOM_TOPIC_MAX_CHARS} disabled={!editable || !onSetTopic}
          placeholder="Sell 200 loaves through the new site by 1 Dec. Always cc sam@acme.com on customer email."
          onChange={(e) => setTopic(e.target.value)}
          onBlur={() => { if (topicDirty) void save("topic", () => onSetTopic!(topic.trim() || null), "The instructions could not be saved."); }} />
        <span className="project-count">{topic.length.toLocaleString()} / {ROOM_TOPIC_MAX_CHARS.toLocaleString()}</span>
      </div>
    </div>
  </>;

  return <>
    <nav className="project-settings__nav" aria-label="Settings sections">
      {SECTIONS.map((s) => <button key={s.id} type="button" aria-current={s.id === section ? "page" : undefined} onClick={() => onSection(s.id)}>{s.label}</button>)}
    </nav>
    <label className="sr-only" htmlFor={`${id}-section`}>Section</label>
    <select id={`${id}-section`} className="project-settings__select room-input" value={section} onChange={(e) => onSection(e.target.value as SettingsSection)}>
      {SECTIONS.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
    </select>
    <div className="project-settings__body">
      <DialogDescription className="sr-only">Settings for the {roomName} project.</DialogDescription>
      {section === "general" ? general : section === "memory" ? memory : <><h3>People</h3><div className="pt-3">{people}</div></>}
      {error && <p role="alert" className="mt-3 text-ui text-err break-words">{error}</p>}
    </div>
  </>;
}
