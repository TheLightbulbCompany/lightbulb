"use client";

// Project settings (UX slice 6): a modal over the project, so Esc puts you back exactly where you were (pane, scroll,
// draft). General (name and icon, Pause, Archive, Delete) · Memory (the Instructions every task here reads) · People
// (the Members content, handed in). Each field saves on change or blur and says "Saved" beside itself for 2 s.
// "What Lightbulb remembers here" lists the facts the Router noted from this project's messages (W15-5). Memory-engine
// facts carry no project tag (08 §6c spike), so the noted messages are the exact, honest list.

import { useEffect, useId, useRef, useState, type MutableRefObject, type ReactElement, type ReactNode } from "react";
import { useOrganization } from "@clerk/nextjs";
import { X } from "lucide-react";
import { ROOM_TOPIC_MAX_CHARS, type MessagingClient, type NotedFact, type PatchRoomRequest, type RoomSummary } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { notifyApiError } from "@/lib/notify";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { roomNoun } from "@/components/messaging/types";
import { ProjectIconPicker } from "./ProjectTile";

export type SettingsSection = "general" | "memory" | "people";
/** Where ⋯ › Settings was chosen outside the project (focus goes back there on close). */
export type SettingsFrom = "sidebar" | "home";
const SECTIONS: { id: SettingsSection; label: string }[] = [
  { id: "general", label: "General" }, { id: "memory", label: "Memory" }, { id: "people", label: "People" },
];
const SAVED_MS = 2000;
const UNDO_MS = 5000; // a forgotten fact stays in its row with Undo this long before it is really forgotten (W16-7)

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
  /** A channel's owner or admin, or any member of a DM or group chat with Lightbulb: icon, Instructions, Pause, Forget. */
  canEdit: boolean;
  actionable: boolean;
  onRename?: (name: string) => Promise<void>;
  onSetTopic?: (topic: string | null) => Promise<void>;
  onPatch?: (request: PatchRoomRequest) => Promise<void>;
  /** Open the header's own confirm dialogs (this modal closes first). */
  onArchive?: () => void;
  onDelete?: () => void;
  people: ReactNode;
  /** Reads and forgets what Lightbulb noted here; without it the list is not shown. */
  memoryClient?: Pick<MessagingClient, "listRoomMemory" | "forgetRoomMemory">;
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
        <DialogTitle asChild><h2 className="truncate">{roomName} · {props.room.kind === "channel" ? "Channel" : "Chat"} settings</h2></DialogTitle>
        <DialogClose asChild><Button type="button" variant="ghost" size="icon-sm" aria-label="Close settings"><X aria-hidden="true" /></Button></DialogClose>
      </div>
      {section && <SettingsBody {...props} section={section} flush={flush} />}
    </DialogContent>
  </Dialog>;
}

function SettingsBody({ room, roomName, section, onSection, canEdit, actionable, onRename, onSetTopic, onPatch, onArchive, onDelete, people, memoryClient, flush }: ProjectSettingsProps & { section: SettingsSection; flush: Flush }): ReactElement {
  const id = useId();
  const { membership } = useOrganization();
  // The backend lets a channel's owner/admin pause it, and a workspace admin too (pause alone); a chat with Lightbulb is
  // its people's (`canEdit`), never an admin's. Never #general or the channel shared with the Lightbulb team.
  const canPause = !!onPatch && (canEdit || (room.kind === "channel" && membership?.role === "org:admin")) && !room.isDefault && !room.shared;
  const noun = roomNoun(room.kind);
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
        <p className="project-row__hint">How this {noun} shows in the sidebar and on Home.</p>
      </div>
      <div className="project-row__control">
        <Saved token={saved.name ?? 0} />
        <div className="project-field">
          <ProjectIconPicker icon={room.icon} color={room.color} name={name} chat={room.kind !== "channel"} disabled={!editable || !onPatch}
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
        <p className="project-row__label">{paused ? "Lightbulb is paused" : "Pause Lightbulb"}</p>
        <p className="project-row__hint">Lightbulb stops replying and starting tasks or schedules here. People keep chatting. Running tasks finish.</p>
      </div>
      <div className="project-row__control">
        <Saved token={saved.pause ?? 0} />
        <Button type="button" size="sm" variant="outline" className="h-7" disabled={!actionable || busy}
          onClick={() => void save("pause", () => onPatch!({ paused: !paused }), paused ? `The ${noun} could not be resumed.` : `The ${noun} could not be paused.`)}>
          {paused ? "Resume" : "Pause"}</Button>
      </div>
    </div>}
    {onArchive && <div className="project-row">
      <div className="project-row__text">
        <p className="project-row__label">Archive {noun}</p>
        <p className="project-row__hint">Hides it from the sidebar. Undo any time.</p>
      </div>
      <div className="project-row__control"><Button type="button" size="sm" variant="outline" className="h-7" disabled={!actionable} onClick={onArchive}>Archive</Button></div>
    </div>}
    {/* Off the first screen (W16-9): Delete sits apart at the bottom, an outline button, its confirm still asks. */}
    {onDelete && <section className="project-danger" aria-labelledby={`${id}-danger`}>
      <h3 id={`${id}-danger`}>Danger zone</h3>
      <div className="project-row">
        <div className="project-row__text">
          <p className="project-row__label">Delete {noun}</p>
          <p className="project-row__hint">Removes it and its chat for everyone. Can&apos;t be undone.</p>
        </div>
        <div className="project-row__control"><Button type="button" size="sm" variant="outline" className="project-danger__button h-7" disabled={!actionable} onClick={onDelete}>Delete</Button></div>
      </div>
    </section>}
  </>;

  const memory = <>
    <h3>Memory</h3>
    <div className="project-row">
      <div className="project-row__text grid gap-2">
        <div className="flex items-baseline gap-2">
          <p className="project-row__label mr-auto"><label htmlFor={`${id}-topic`}>Instructions</label></p>
          <Saved token={saved.topic ?? 0} />
        </div>
        <p id={`${id}-topic-hint`} className="project-row__hint">Lightbulb and the specialists read this before every task in this {noun}.</p>
        <textarea id={`${id}-topic`} className="project-textarea" style={{ minHeight: 120 }} aria-describedby={`${id}-topic-hint`}
          value={topic} maxLength={ROOM_TOPIC_MAX_CHARS} disabled={!editable || !onSetTopic}
          placeholder="Sell 200 loaves through the new site by 1 Dec. Always cc sam@acme.com on customer email."
          onChange={(e) => setTopic(e.target.value)}
          onBlur={() => { if (topicDirty) void save("topic", () => onSetTopic!(topic.trim() || null), "The instructions could not be saved."); }} />
        <span className="project-count">{topic.length.toLocaleString()} / {ROOM_TOPIC_MAX_CHARS.toLocaleString()}</span>
      </div>
    </div>
    {memoryClient && <Remembered roomId={room.roomId} noun={noun} client={memoryClient} canForget={canEdit && actionable} />}
  </>;

  return <>
    <nav className="project-settings__nav" aria-label="Settings sections">
      {SECTIONS.map((s) => <button key={s.id} type="button" aria-current={s.id === section ? "page" : undefined} onClick={() => onSection(s.id)}>{s.label}</button>)}
    </nav>
    <label className="sr-only" htmlFor={`${id}-section`}>Section</label>
    <select id={`${id}-section`} className="project-settings__select" value={section} onChange={(e) => onSection(e.target.value as SettingsSection)}>
      {SECTIONS.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
    </select>
    <div className="project-settings__body">
      <DialogDescription className="sr-only">{room.kind === "channel" ? `Settings for the #${roomName} channel.` : `Settings for ${roomName}.`}</DialogDescription>
      {section === "general" ? general : section === "memory" ? memory : <><h3>People</h3><div className="pt-3">{people}</div></>}
      {error && <p role="alert" className="mt-3 text-ui text-err break-words">{error}</p>}
    </div>
  </>;
}

/** What Lightbulb noted from this project's messages, newest first; whoever runs it can forget one. Forget keeps the
 *  row where it is, struck through with Undo in Forget's place, for 5 s (W16-7): nothing slides under the pointer, a
 *  slip is undone, and a double click undoes rather than forgetting a second fact. Closing settings forgets for real. */
function Remembered({ roomId, noun, client, canForget }: { roomId: string; noun: "channel" | "chat"; client: NonNullable<ProjectSettingsProps["memoryClient"]>; canForget: boolean }): ReactElement {
  const headingId = useId();
  const [items, setItems] = useState<NotedFact[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [undoable, setUndoable] = useState<ReadonlySet<string>>(new Set());
  // Each pending forget: its timer and the call that commits it (run early when settings close).
  const pending = useRef(new Map<string, { timer: ReturnType<typeof setTimeout>; commit: () => void }>());
  const open = useRef(true);
  useEffect(() => {
    let live = true;
    client.listRoomMemory(roomId).then((r) => { if (live) setItems(r.items); },
      () => { if (live) setError("What Lightbulb remembers couldn't be loaded. Close and open Settings to try again."); });
    return () => { live = false; };
  }, [client, roomId]);
  useEffect(() => {
    const all = pending.current;
    open.current = true;
    return () => { open.current = false; for (const { timer, commit } of all.values()) { clearTimeout(timer); commit(); } all.clear(); };
  }, []);
  const settle = (id: string) => { pending.current.delete(id); setUndoable((s) => { const n = new Set(s); n.delete(id); return n; }); };
  const forget = (item: NotedFact) => {
    setError(null);
    const commit = () => {
      client.forgetRoomMemory(roomId, item.messageId).then(
        () => setItems((all) => all && all.filter((i) => i.messageId !== item.messageId)),
        // Still open: say so beside the list. Closed meanwhile: a toast, where the person now looks.
        (e) => { if (open.current) setError("That couldn't be forgotten. Try again."); else notifyApiError(e, "That couldn't be forgotten."); });
    };
    const timer = setTimeout(() => { settle(item.messageId); commit(); }, UNDO_MS);
    pending.current.set(item.messageId, { timer, commit });
    setUndoable((s) => new Set(s).add(item.messageId));
  };
  const undo = (item: NotedFact) => {
    const p = pending.current.get(item.messageId);
    if (p) clearTimeout(p.timer);
    settle(item.messageId);
  };
  return <section className="project-row project-memory" aria-labelledby={headingId}>
    <div className="project-row__text grid gap-2">
      <p id={headingId} className="project-row__label">What Lightbulb remembers here</p>
      {items === null && !error && <p className="project-row__hint">Loading…</p>}
      {items?.length === 0 && <p className="project-row__hint">Tell Lightbulb things about this {noun} and they&apos;ll show up here.</p>}
      {!!items?.length && <ul className="project-memory__list">
        {items.map((item) => {
          const gone = undoable.has(item.messageId);
          return <li key={item.messageId} className="project-memory__item" data-forgotten={gone || undefined}>
            <span className="project-memory__fact">{item.fact}</span>
            {gone ? <span role="status" className="project-memory__when">Forgotten</span>
              : <time className="project-memory__when" dateTime={item.notedAt}>{timeAgo(item.notedAt)}</time>}
            {canForget && (gone
              ? <Button type="button" variant="ghost" size="sm" className="project-memory__forget h-6 px-2"
                  aria-label={`Undo forget: ${item.fact}`} onClick={() => undo(item)}>Undo</Button>
              : <Button type="button" variant="ghost" size="sm" className="project-memory__forget h-6 px-2"
                  aria-label={`Forget: ${item.fact}`} onClick={() => forget(item)}>Forget</Button>)}
          </li>;
        })}
      </ul>}
      {error && <p role="alert" className="text-ui text-err">{error}</p>}
    </div>
  </section>;
}
