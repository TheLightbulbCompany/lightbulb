"use client";

import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { DndContext, KeyboardSensor, PointerSensor, closestCenter, useSensor, useSensors, type DragEndEvent } from "@dnd-kit/core";
import { SortableContext, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { ChevronDown, ChevronRight, FolderPlus, GripVertical, Pencil, Plus, Trash2, Users } from "lucide-react";
import type { ListenMode, MemberPrefsRequest, RoomKind, RoomSection } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Identity } from "@/components/teams/shared/components/Identity";
import { copyText, roomPermalink } from "./messageLinks";
import { nextUnreadRoomId } from "./roomNavigation";
import { RoomDirectoryRow } from "./RoomDirectoryRow";
import { groupRooms, type SidebarSort } from "./roomSections";
import { readSidebarPrefs, writeSidebarPrefs, type SidebarPrefs } from "./sidebarPrefs";
import { useAsyncAction } from "./useAsyncAction";
import { roomDisplayName, type DirectoryParticipant, type ParticipantSelection, type DirectoryRoom } from "./types";
import "./messaging.css";

export interface ConversationCreation {
  kind: RoomKind;
  name: string | null;
  members: ParticipantSelection[];
}

export interface RoomDirectoryProps {
  ownerScopeId: string;
  rooms: readonly DirectoryRoom[];
  sections: readonly RoomSection[];
  participants: readonly DirectoryParticipant[];
  state: "loading" | "ready" | "error" | "access-removed";
  currentRoomId?: string;
  onSelectRoom: (roomId: string) => void;
  onSelectParticipant: (participant: DirectoryParticipant) => void | Promise<void>;
  onCreate?: (creation: ConversationCreation) => void | Promise<boolean>;
  onMarkUnread?: (roomId: string) => void | Promise<void>;
  onSetPrefs?: (roomId: string, prefs: MemberPrefsRequest) => Promise<void>;
  onSaveSections?: (sections: RoomSection[]) => Promise<void>;
  onLeaveRoom?: (roomId: string) => Promise<void>;
  creationBusy?: boolean;
  creationError?: string;
  onRetry?: () => void;
  draftRoomIds?: readonly string[];
}

// Mirrors the backend's own limits (`core/services/messaging.py`) so the
// dialog refuses what a PUT would reject anyway, instead of round-tripping a
// 422 the owner cannot act on.
const SECTION_NAME_MAX_CHARS = 40;
const MAX_SECTIONS = 20;

export function RoomDirectory(props: RoomDirectoryProps) {
  return <DirectoryContent key={JSON.stringify([props.ownerScopeId, props.currentRoomId])} {...props} />;
}

/** A custom section: the whole group moves, dragged by the grip in its header. */
function SortableSection({ id, label, children }: { id: string; label: string; children: (grip: ReactNode) => ReactNode }) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id });
  const grip = <button type="button" className="room-directory-grip" aria-label={`Reorder ${label}`} {...attributes} {...listeners}>
    <GripVertical size={14} aria-hidden="true" />
  </button>;
  return <section ref={setNodeRef} aria-label={label} data-dragging={isDragging || undefined}
    style={{ transform: CSS.Transform.toString(transform), transition }}>{children(grip)}</section>;
}

function DirectoryContent({
  ownerScopeId,
  rooms,
  sections: savedSections,
  participants,
  state,
  currentRoomId,
  onSelectRoom,
  onSelectParticipant,
  onCreate,
  onMarkUnread,
  onSetPrefs,
  onSaveSections,
  onLeaveRoom,
  creationBusy = false,
  creationError,
  onRetry,
  draftRoomIds = [],
}: RoomDirectoryProps) {
  const id = useId();
  const [creating, setCreating] = useState(false);
  const [kind, setKind] = useState<RoomKind>("channel");
  const [name, setName] = useState("");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(new Map<string, ParticipantSelection>());
  const [directoryOpen, setDirectoryOpen] = useState(false);
  const [markingUnread, setMarkingUnread] = useState<string | null>(null);
  const [unreadError, setUnreadError] = useState<{ roomId: string; label: string } | null>(null);
  const [prefs, setPrefs] = useState(() => readSidebarPrefs(ownerScopeId));
  const savePrefs = useAsyncAction();
  const saveSections = useAsyncAction();
  const leave = useAsyncAction();
  const [managing, setManaging] = useState(false);
  // Only the NAMES being edited are local: the rows themselves stay the
  // server's list, so a section another device added shows up mid-edit
  // instead of being silently dropped by the next save.
  const [nameEdits, setNameEdits] = useState<Record<string, string>>({});
  const [newSection, setNewSection] = useState("");
  const [deleting, setDeleting] = useState<RoomSection | null>(null);
  const [leaving, setLeaving] = useState<DirectoryRoom | null>(null);
  // The order shown while a drag is being saved; cleared either way, so a
  // failed save falls back to the server's order and a successful one lands
  // on the list the save returned.
  const [pendingOrder, setPendingOrder] = useState<RoomSection[] | null>(null);
  const deleteCancel = useRef<HTMLButtonElement>(null);
  const leaveCancel = useRef<HTMLButtonElement>(null);
  const sections = pendingOrder ?? savedSections;
  const drafts = sections.map((section) => ({ ...section, name: nameEdits[section.id] ?? section.name }));
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );
  // The row `savePrefs` is acting on, so one in-flight star doesn't disable
  // every other row's trigger. Only read while `savePrefs.pending`, so a
  // stale value after the action settles is inert.
  const [actingRoomId, setActingRoomId] = useState<string | null>(null);
  const validName = kind === "channel"
    ? name.trim().length >= 1 && name.trim().length <= 80
    : kind === "group-dm" ? name.trim().length <= 80 : true;
  const validCount = kind === "dm" ? selected.size === 1 : kind === "group-dm" ? selected.size >= 2 : true;
  const canCreate = !!onCreate && validName && validCount && selected.size <= 31;
  const visibleParticipants = participants.filter((participant) => {
    const needle = query.toLocaleLowerCase();
    return participant.displayName.toLocaleLowerCase().includes(needle) || participant.mentionHandle?.includes(needle);
  });
  const partialError = state === "error" && rooms.length > 0;
  const unreadRooms = rooms.filter((room) => room.unreadCount > 0);
  // A drafted room can be absent from `rooms` (left, or directory still
  // loading) — gate the section on this filtered list, not on
  // draftRoomIds.length, or the heading renders over an empty list.
  const draftedRooms = rooms.filter((room) => draftRoomIds.includes(room.roomId));
  const groups = groupRooms(rooms, sections, { sort: prefs.sort, nameOf: (room) => roomDisplayName(room, room.participants) });

  // The delete confirm replaces the dialog's controls in place, so focus has
  // to follow it — and land on Cancel, never on the destructive button.
  useEffect(() => { if (deleting) deleteCancel.current?.focus(); }, [deleting]);

  async function persistSections(next: RoomSection[]) {
    if (!onSaveSections) return false;
    let saved = false;
    await saveSections.run(() => onSaveSections(next), "Sections could not be saved.", () => { saved = true; });
    return saved;
  }

  function reorderSections(event: DragEndEvent) {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    const ids = sections.map((section) => section.id);
    const from = ids.indexOf(String(active.id));
    const to = ids.indexOf(String(over.id));
    if (from < 0 || to < 0) return;
    const next = [...sections];
    next.splice(to, 0, ...next.splice(from, 1));
    setPendingOrder(next);
    // Only this save's order is cleared — a second drag while the first is in
    // flight owns the list from then on.
    void persistSections(next).then(() => setPendingOrder((current) => (current === next ? null : current)));
  }

  function openManageSections() {
    saveSections.reset();
    setNameEdits({});
    setNewSection("");
    setDeleting(null);
    setManaging(true);
  }

  function setRoomPrefs(roomId: string, next: MemberPrefsRequest, fallback: string) {
    if (!onSetPrefs) return;
    setActingRoomId(roomId);
    void savePrefs.run(() => onSetPrefs(roomId, next), fallback);
  }

  const updatePrefs = (patch: Partial<SidebarPrefs>) => {
    const next = { ...prefs, ...patch };
    setPrefs(next);
    writeSidebarPrefs(ownerScopeId, next);
  };

  function selectNextUnread() {
    const next = nextUnreadRoomId(rooms, currentRoomId ?? null, 1);
    if (next) onSelectRoom(next);
  }

  async function markRoomUnread(roomId: string, label: string) {
    if (!onMarkUnread || markingUnread) return;
    setMarkingUnread(roomId);
    setUnreadError(null);
    try {
      await onMarkUnread(roomId);
    } catch {
      setUnreadError({ roomId, label });
    } finally {
      setMarkingUnread(null);
    }
  }

  if (state !== "ready" && !partialError) return <nav className="room-directory" aria-label="Conversations">
    <h2 className="font-semibold">Conversations</h2>
    <p role="status">{state === "loading" ? "Loading conversations…" : state === "access-removed" ? "Access removed" : "Conversations could not be loaded."}</p>
    {state === "loading" && <div aria-hidden="true" className="room-skeleton"><span /><span /><span /></div>}
    {state === "error" && onRetry && <Button variant="outline" onClick={onRetry}>Try again</Button>}
  </nav>;

  function toggleParticipant(participant: DirectoryParticipant, checked: boolean) {
    setSelected((current) => {
      const next = new Map(current);
      const key = `${participant.kind}:${participant.id}`;
      if (!checked) next.delete(key);
      else next.set(key, {
        kind: participant.kind,
        id: participant.id,
        role: "member",
        listenMode: participant.kind === "agent" ? "mentions" : null,
      });
      return next;
    });
  }

  function setListenMode(participant: DirectoryParticipant, listenMode: ListenMode) {
    const key = `${participant.kind}:${participant.id}`;
    setSelected((current) => {
      const selection = current.get(key);
      if (!selection || selection.kind !== "agent") return current;
      return new Map(current).set(key, { ...selection, listenMode });
    });
  }

  return <Dialog open={creating} onOpenChange={(open) => { if (!creationBusy) setCreating(open); }}><nav className="room-directory" aria-label="Conversations">
    <div className="room-directory-heading"><h2 className="font-semibold">Conversations</h2>
      <div className="room-directory-heading-actions">
        <label className="sr-only" htmlFor={`${id}-sort`}>Sort conversations</label>
        <select id={`${id}-sort`} className="room-listen-mode" value={prefs.sort}
          onChange={(event) => updatePrefs({ sort: event.target.value as SidebarSort })}>
          <option value="alpha">A–Z</option>
          <option value="unread">Unread first</option>
        </select>
        <Button variant="ghost" size="icon" aria-label="Next unread conversation" disabled={unreadRooms.length === 0} onClick={selectNextUnread}><ChevronDown aria-hidden="true" /></Button>
        {onSaveSections && <Button variant="ghost" size="icon" aria-label="Manage sections" onClick={openManageSections}><FolderPlus aria-hidden="true" /></Button>}
        {onCreate && <DialogTrigger asChild><Button variant="ghost" size="icon" aria-label="New conversation" disabled={creationBusy}><Plus aria-hidden="true" /></Button></DialogTrigger>}
      </div>
    </div>
    {unreadError && <p role="alert" className="px-2 text-sm text-err">Could not mark {unreadError.label} unread. <Button variant="outline" disabled={markingUnread !== null}
      onClick={() => void markRoomUnread(unreadError.roomId, unreadError.label)}>Try again</Button></p>}
    {partialError && <div role="alert" className="px-2 text-sm text-err">Some conversation details could not be loaded. {onRetry && <Button variant="outline" onClick={onRetry}>Try again</Button>}</div>}
    {savePrefs.error && <p role="alert" className="px-2 text-sm text-err">{savePrefs.error}</p>}
    {saveSections.error && !managing && <p role="alert" className="px-2 text-sm text-err">{saveSections.error}</p>}
    {groups.length === 0 && <p className="text-sm text-muted-foreground px-2">Create a channel, or start a conversation with a person or agent.</p>}
    <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={reorderSections}>
    <SortableContext items={sections.map((section) => section.id)} strategy={verticalListSortingStrategy}>
    {groups.map((group) => {
      const collapsed = prefs.collapsed.includes(group.id);
      // Collapsing hides rooms, never activity or your place: the header keeps
      // the group's unread count, and the room you are reading stays on screen
      // so `aria-current` survives a ⌘K / next-unread / permalink jump into a
      // collapsed group.
      const unread = group.rooms.reduce((total, room) => total + (room.muted ? 0 : room.unreadCount), 0);
      const visible = collapsed ? group.rooms.filter((room) => room.roomId === currentRoomId) : group.rooms;
      const listId = `${id}-group-${group.id}`;
      const body = (grip?: ReactNode) => <>
        <div className="room-directory-group-header">
          {grip}
          <h3 className="room-directory-group">{group.label}</h3>
          {collapsed && unread > 0 && <span className="room-count" aria-label={`${unread} unread messages in ${group.label}`}>{unread}</span>}
          <Button variant="ghost" size="icon-sm" aria-expanded={!collapsed} aria-controls={listId}
            aria-label={`${collapsed ? "Expand" : "Collapse"} ${group.label}`}
            onClick={() => updatePrefs({ collapsed: collapsed ? prefs.collapsed.filter((groupId) => groupId !== group.id) : [...prefs.collapsed, group.id] })}>
            {collapsed ? <ChevronRight aria-hidden="true" /> : <ChevronDown aria-hidden="true" />}
          </Button>
        </div>
        <ul id={listId}>{visible.map((room) => {
          const label = roomDisplayName(room, room.participants);
          return <RoomDirectoryRow key={room.roomId} room={room} label={label} sections={sections}
            active={room.roomId === currentRoomId} draft={draftRoomIds.includes(room.roomId)}
            busy={markingUnread === room.roomId || (savePrefs.pending && actingRoomId === room.roomId)}
            onSelect={() => onSelectRoom(room.roomId)}
            onToggleStar={onSetPrefs && (() => setRoomPrefs(room.roomId, { starred: !room.starred }, `Could not ${room.starred ? "unstar" : "star"} ${label}.`))}
            onToggleMute={onSetPrefs && (() => setRoomPrefs(room.roomId, { muted: !room.muted }, `Could not ${room.muted ? "unmute" : "mute"} ${label}.`))}
            onMarkUnread={onMarkUnread && (() => void markRoomUnread(room.roomId, label))}
            onMoveToSection={onSetPrefs && sections.length > 0
              ? (sectionId) => setRoomPrefs(room.roomId, { sectionId }, `Could not move ${label}.`) : undefined}
            onLeave={onLeaveRoom && room.kind === "channel" ? () => { leave.reset(); setLeaving(room); } : undefined}
            onCopyLink={() => void copyText(roomPermalink(room.roomId))} />;
        })}</ul>
      </>;
      return group.custom
        ? <SortableSection key={group.id} id={group.id} label={group.label}>{body}</SortableSection>
        : <section key={group.id} aria-label={group.label}>{body()}</section>;
    })}
    </SortableContext>
    </DndContext>
    {draftedRooms.length > 0 && <section aria-label="Drafts">
      <h3 className="room-directory-group">Drafts</h3>
      <ul>{draftedRooms.map((room) => {
        const label = roomDisplayName(room, room.participants);
        return <li key={`draft:${room.roomId}`} className="room-directory-row">
          <button type="button" className="room-directory-entry" aria-current={room.roomId === currentRoomId ? "page" : undefined}
            onClick={() => onSelectRoom(room.roomId)}>
            {/* The "Drafts" heading already says it — an "Unsent draft" label on every row is noise. */}
            <Pencil size={16} aria-hidden="true" />
            <span className="min-w-0 flex-1"><span className="block truncate" title={label}>{label}</span></span>
          </button>
        </li>;
      })}</ul>
    </section>}
    <section aria-label="People and agents">
      <Button variant="ghost" className="w-full justify-start" aria-expanded={directoryOpen}
        aria-controls={`${id}-people`} onClick={() => setDirectoryOpen(!directoryOpen)}><Users aria-hidden="true" />People and agents</Button>
      {directoryOpen && <div id={`${id}-people`} className="room-picker">
        <label htmlFor={`${id}-directory-search`} className="sr-only">Find people and agents</label>
        <input id={`${id}-directory-search`} className="room-input" type="search" placeholder="Find a person or agent…" value={query} onChange={(event) => setQuery(event.target.value)} />
        <div className="room-picker-list">{visibleParticipants.map((participant) => <button type="button" className="room-choice room-choice-button" key={`${participant.kind}:${participant.id}`} disabled={creationBusy} onClick={() => onSelectParticipant(participant)}>
          <Identity name={participant.displayName} avatarOnly size="sm" />
          <span className="min-w-0 flex-1"><span className="block truncate">{participant.displayName}</span><span className="text-sm text-muted-foreground">{participant.kind === "agent" ? participant.mentionHandle ? `@${participant.mentionHandle}` : "Agent" : "Person"}</span></span>
        </button>)}</div>
        {visibleParticipants.length === 0 && <p className="text-sm text-muted-foreground">No matching people or agents.</p>}
        {creationError && <p role="alert" className="text-sm text-err">{creationError}</p>}
      </div>}
    </section>
    <DialogContent className="room-creation-dialog" showCloseButton={!creationBusy}>
    <form aria-label="Create conversation" className="room-creation" aria-busy={creationBusy}
      onSubmit={async (event) => {
        event.preventDefault();
        if (!canCreate || creationBusy) return;
        const created = await onCreate?.({ kind, name: kind === "dm" ? null : name.trim() || null, members: [...selected.values()] });
        if (created) { setCreating(false); setName(""); setSelected(new Map()); setQuery(""); setKind("channel"); }
      }}>
      <div className="room-creation-header">
        <DialogTitle>{kind === "channel" ? "Create a channel" : kind === "group-dm" ? "New group chat" : "New direct message"}</DialogTitle>
        <DialogDescription>Choose the people and agents you want to talk with.</DialogDescription>
      </div>
      <div className="room-creation-body">
      <label htmlFor={`${id}-kind`}>Conversation type</label>
      <select id={`${id}-kind`} className="room-input" value={kind} disabled={creationBusy}
        onChange={(event) => { setKind(event.target.value as RoomKind); setSelected(new Map()); }}>
        <option value="channel">Channel</option>
        <option value="dm">Direct message</option>
        <option value="group-dm">Group chat</option>
      </select>
      {kind !== "dm" && <><label htmlFor={`${id}-name`}>{kind === "channel" ? "Channel name" : "Group chat name (optional)"}</label>
        <input id={`${id}-name`} className="room-input" value={name} maxLength={80} disabled={creationBusy} onChange={(event) => setName(event.target.value)} /></>}
      <p className="text-sm text-muted-foreground">Only selected workspace members can read this conversation.</p>
      <div className="room-picker">
        <p className="text-sm font-semibold">People and agents · {selected.size} selected</p>
        <label htmlFor={`${id}-search`} className="sr-only">Find people and agents</label>
        <input id={`${id}-search`} className="room-input" type="search" placeholder="Find a person or agent…" value={query} onChange={(event) => setQuery(event.target.value)} />
        <div className="room-picker-list">{visibleParticipants.map((participant) => {
          const key = `${participant.kind}:${participant.id}`;
          const selection = selected.get(key);
          const content = <>
            <input aria-label={`Select ${participant.displayName}`} type="checkbox" checked={!!selection} disabled={creationBusy || (!selection && selected.size >= 31)} onChange={(event) => toggleParticipant(participant, event.target.checked)} />
            <Identity name={participant.displayName} avatarOnly size="sm" />
            <span className="min-w-0 flex-1"><span className="block truncate" title={participant.displayName}>{participant.displayName}</span>
              <span className="text-sm text-muted-foreground">{participant.kind === "agent" ? participant.mentionHandle ? `@${participant.mentionHandle}` : "Agent" : "Person"}</span></span>
            {selection?.kind === "agent" && <label className="sr-only" htmlFor={`${id}-${participant.id}-listen`}>Response for {participant.displayName}</label>}
            {selection?.kind === "agent" && <select id={`${id}-${participant.id}-listen`} className="room-listen-mode" value={selection.listenMode ?? "mentions"} disabled={creationBusy}
              onChange={(event) => setListenMode(participant, event.target.value as ListenMode)}>
              <option value="active">Automatic</option><option value="mentions">Mentions only</option><option value="observe">Observe</option>
            </select>}</>;
          return <div className="room-choice" key={key}>{content}</div>;
        })}</div>
        {visibleParticipants.length === 0 && <p className="text-sm text-muted-foreground">No matching people or agents.</p>}
      </div>
      </div>
      <div className="room-creation-footer">
        {creationError && <p role="alert" className="text-sm text-err">{creationError}</p>}
        <div className="room-composer-actions"><Button type="button" variant="ghost" disabled={creationBusy} onClick={() => setCreating(false)}>Cancel</Button>
          <Button type="submit" disabled={!canCreate || creationBusy}>{creationBusy ? "Creating…" : kind === "channel" ? "Create channel" : kind === "group-dm" ? "Start group chat" : "Start direct message"}</Button></div>
      </div>
    </form>
    </DialogContent>
    <Dialog open={managing} onOpenChange={(open) => { if (!open && !saveSections.pending) { setManaging(false); saveSections.reset(); } }}>
      <DialogContent className="room-section-dialog max-w-[calc(100%-2rem)] sm:max-w-md max-h-[calc(100dvh-2rem)] overflow-y-auto">
        <DialogHeader><DialogTitle>Manage sections</DialogTitle>
          <DialogDescription>Group conversations however you work. Deleting a section returns its conversations to Channels or Direct messages.</DialogDescription>
        </DialogHeader>
        {sections.length > 0 && <ul className="grid gap-2">{sections.map((section) => <li key={section.id} className="room-section-edit">
          {/* Both accessible names stay on the SAVED name: a control whose name
              changes under the caret as you type is unusable with a screen reader. */}
          <label className="sr-only" htmlFor={`${id}-section-${section.id}`}>Rename {section.name}</label>
          <input id={`${id}-section-${section.id}`} className="room-input" value={nameEdits[section.id] ?? section.name}
            maxLength={SECTION_NAME_MAX_CHARS} disabled={saveSections.pending || !!deleting}
            onChange={(event) => setNameEdits({ ...nameEdits, [section.id]: event.target.value })} />
          <Button type="button" variant="ghost" size="icon" aria-label={`Delete ${section.name}`}
            disabled={saveSections.pending || !!deleting} onClick={() => { saveSections.reset(); setDeleting(section); }}><Trash2 aria-hidden="true" /></Button>
        </li>)}</ul>}
        <form className="room-section-edit" onSubmit={async (event) => {
          event.preventDefault();
          const name = newSection.trim();
          if (!name || sections.length >= MAX_SECTIONS) return;
          if (await persistSections([...drafts, { id: crypto.randomUUID(), name }])) { setNewSection(""); setNameEdits({}); }
        }}>
          <label className="sr-only" htmlFor={`${id}-new-section`}>New section name</label>
          <input id={`${id}-new-section`} className="room-input" value={newSection} maxLength={SECTION_NAME_MAX_CHARS}
            disabled={saveSections.pending || !!deleting} onChange={(event) => setNewSection(event.target.value)} />
          <Button type="submit" variant="outline" disabled={!newSection.trim() || sections.length >= MAX_SECTIONS || saveSections.pending || !!deleting}>Add section</Button>
        </form>
        {sections.length >= MAX_SECTIONS && <p className="text-sm text-muted-foreground">You can keep up to {MAX_SECTIONS} sections.</p>}
        {drafts.some((section) => !section.name.trim()) && <p className="text-sm text-muted-foreground">Every section needs a name.</p>}
        {saveSections.error && <p role="alert" className="text-sm text-err break-words">{saveSections.error}</p>}
        {deleting && <div role="alert" className="room-section-confirm">
          <p className="text-sm">Delete {deleting.name}? Its conversations move back to Channels or Direct messages.</p>
          <div className="room-composer-actions">
            <Button ref={deleteCancel} type="button" variant="outline" disabled={saveSections.pending} onClick={() => { setDeleting(null); saveSections.reset(); }}>Cancel</Button>
            <Button type="button" variant="destructive" disabled={saveSections.pending}
              onClick={async () => { if (await persistSections(drafts.filter((section) => section.id !== deleting.id))) { setDeleting(null); setNameEdits({}); } }}>
              {saveSections.pending ? "Deleting…" : "Delete section"}</Button>
          </div>
        </div>}
        {!deleting && <DialogFooter className="gap-2">
          <Button type="button" variant="outline" disabled={saveSections.pending} onClick={() => { setNameEdits({}); setManaging(false); saveSections.reset(); }}>Cancel</Button>
          <Button type="button" disabled={saveSections.pending || drafts.some((section) => !section.name.trim())}
            onClick={async () => { if (await persistSections(drafts.map((section) => ({ ...section, name: section.name.trim() })))) { setNameEdits({}); setManaging(false); } }}>
            {saveSections.pending ? "Saving…" : "Save sections"}</Button>
        </DialogFooter>}
      </DialogContent>
    </Dialog>
    <Dialog open={!!leaving} onOpenChange={(open) => { if (!open && !leave.pending) { setLeaving(null); leave.reset(); } }}>
      <DialogContent className="room-section-dialog max-w-[calc(100%-2rem)] sm:max-w-md"
        onOpenAutoFocus={(event) => { event.preventDefault(); leaveCancel.current?.focus(); }}>
        <DialogHeader><DialogTitle className="break-words">Leave {leaving ? roomDisplayName(leaving, leaving.participants) : ""}?</DialogTitle>
          <DialogDescription>You will stop receiving its messages and lose access to its history. An admin can add you back.</DialogDescription>
        </DialogHeader>
        {leave.error && <p role="alert" className="text-sm text-err break-words">{leave.error}</p>}
        <DialogFooter className="gap-2">
          <Button ref={leaveCancel} type="button" variant="outline" disabled={leave.pending} onClick={() => { setLeaving(null); leave.reset(); }}>Cancel</Button>
          <Button type="button" variant="destructive" disabled={leave.pending}
            onClick={() => void leave.run(() => onLeaveRoom!(leaving!.roomId), "The channel could not be left.", () => setLeaving(null))}>
            {leave.pending ? "Leaving…" : "Leave channel"}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </nav></Dialog>;
}
