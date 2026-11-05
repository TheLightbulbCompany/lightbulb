"use client";

import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { DndContext, KeyboardSensor, PointerSensor, closestCenter, useSensor, useSensors, type Announcements, type DragEndEvent } from "@dnd-kit/core";
import { SortableContext, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { ChevronDown, ChevronRight, GripVertical, History, MoreHorizontal, Pencil, Plus, SlidersHorizontal, Trash2 } from "lucide-react";
import { listenModeOptions, roomDisplayName, soleChannelOwner, type ListenMode, type SessionMarks, type SessionRow, type MemberPrefsRequest, type RoomKind, type RoomSection } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuRadioGroup, DropdownMenuRadioItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { ParticipantAvatar } from "./ParticipantAvatar";
import { Identity } from "@/components/teams/shared/components/Identity";
import { copyText, roomPermalink } from "./messageLinks";
import { isUnreadTarget, nextUnreadRoomId } from "./roomNavigation";
import { RoomDirectoryRow } from "./RoomDirectoryRow";
import { groupRooms, type RoomGroup, type SidebarRow, type SidebarSession, type SidebarSort } from "./roomSections";
import { useSuperAgent } from "@/hooks/useLiveEvents";
import { foldedEntries, ownWaitingCount, sessionRowGone, sessionRowGroups, sessionRowKey, NEEDS_YOU_GROUP, SESSION_GROUP_FOLD, type SessionGroupBy, type SessionShow } from "./sessionGroups";
import { SessionListRow } from "./SessionListRow";
import { SessionSearch } from "./SessionSearch";
import { StartSessionDialog } from "./StartSessionDialog";
import { readSessionMarks, readSidebarPrefs, writeSessionMarks, writeSidebarPrefs, type SidebarPrefs } from "./sidebarPrefs";
import { useAsyncAction } from "./useAsyncAction";
import { openInviteDialog } from "@/components/workspace/workspaceController";
import type { DirectoryParticipant, ParticipantSelection, DirectoryRoom } from "./types";
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
  /** `kind:id` of the roster row currently being opened, if any. Only that row
   *  goes busy — opening one conversation must not freeze the whole list. */
  openingParticipant?: string | null;
  creationError?: string;
  onRetry?: () => void;
  draftRoomIds?: readonly string[];
  /** Every row the sidebar groups (backend sessions joined to network ones); archived ones fold away, All sessions lists every one. */
  sessionRows?: readonly SidebarRow[];
  /** Carried for the focus pane; the sidebar and All sessions read `sessionRows` instead. */
  sessions?: readonly SidebarSession[];
  /** Share one of the viewer's own unshared terminal sessions (rows with no backend). */
  onShareSession?: (row: SessionRow) => Promise<unknown>;
  /** Present: the Sessions group always shows, with an empty state when nobody is sharing. */
  onSelectSession?: (sessionId: string) => void;
  onRenameSession?: (sessionId: string, title: string | null) => Promise<unknown>;
  /** The owner takes one of their ended sessions, or all of them, off the list (All sessions and the open session). */
  onRemoveSession?: (sessionId: string) => Promise<unknown>;
  onRemoveEndedSessions?: () => Promise<unknown>;
  /** Mark unread (true) or read (false): moves this viewer's seen baseline for the row's dot. */
  onMarkSessionUnread?: (sessionId: string, unread: boolean) => void;
  currentSessionId?: string | null;
  /** Opens the All sessions list in the main pane. */
  onOpenAllSessions?: () => void;
  allSessionsOpen?: boolean;
  /** The signed-in person: their role in a room decides whether its menu offers Add people. */
  currentUserId?: string;
  /** Opens the room with its member picker open. */
  onAddPeople?: (roomId: string) => void;
  /** Extra header buttons (e.g. "New session"), rendered before "New conversation". */
  headerActions?: ReactNode;
  /** The starter roster's CEO agent (by role, from the agent roster): its DM leads Team. */
  ceoAgentId?: string;
  /** A destination was picked: the shell closes the phone drawer (the Invite row uses it). */
  onNavigate?: () => void;
}

// Mirrors the backend's own limits (`core/services/messaging.py`) so the
// dialog refuses what a PUT would reject anyway, instead of round-tripping a
// 422 the owner cannot act on.
const SECTION_NAME_MAX_CHARS = 40;
const MAX_SECTIONS = 20;
// Groups shown below Sessions; everything else sits above it.
const TAIL_GROUPS = new Set(["external", "archived"]);

export function RoomDirectory(props: RoomDirectoryProps) {
  // Keyed on the owner scope alone: every dependent read (active=, the
  // collapsed-group visible filter) already takes currentRoomId as a plain
  // prop, so including it here only bought an unwanted remount on every room
  // switch — dropping the sidebar's error banner, an in-flight section
  // reorder, and the People-and-agents search mid-use. See
  // `git log -S'currentRoomId])' -- RoomDirectory.tsx`: the key has carried
  // currentRoomId since the file's introduction, and none of DirectoryContent's
  // state (section-management dialogs, the creation form, sidebar prefs) is
  // scoped to which room is open, so nothing needs a reset here.
  return <DirectoryContent key={props.ownerScopeId} {...props} />;
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
  openingParticipant,
  onCreate,
  onMarkUnread,
  onSetPrefs,
  onSaveSections,
  onLeaveRoom,
  creationBusy = false,
  creationError,
  onRetry,
  draftRoomIds = [],
  sessionRows = [],
  onShareSession,
  onSelectSession,
  onRenameSession,
  onMarkSessionUnread,
  currentSessionId = null,
  onOpenAllSessions,
  allSessionsOpen = false,
  headerActions,
  currentUserId,
  onAddPeople,
  ceoAgentId,
  onNavigate,
}: RoomDirectoryProps) {
  const superAgent = useSuperAgent();
  const id = useId();
  const [creating, setCreating] = useState(false);
  const [kind, setKind] = useState<RoomKind>("channel");
  const [name, setName] = useState("");
  const [query, setQuery] = useState("");
  const [sessionFilter, setSessionFilter] = useState("");
  const [archivedOpen, setArchivedOpen] = useState(false);
  // Per person AND workspace: two members on one browser profile keep their own pins and archives.
  // The user id can arrive after the first render, so the marks carry the key they were read under.
  const marksKey = `${currentUserId ?? ""}:${ownerScopeId}`;
  const [storedMarks, setStoredMarks] = useState(() => ({ key: marksKey, marks: readSessionMarks(marksKey) }));
  const sessionMarks = storedMarks.key === marksKey ? storedMarks.marks : readSessionMarks(marksKey);
  const updateSessionMarks = (next: SessionMarks) => { setStoredMarks({ key: marksKey, marks: next }); writeSessionMarks(marksKey, next); };
  const [selected, setSelected] = useState(new Map<string, ParticipantSelection>());
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
  const leavingNoun = leaving?.kind === "group-dm" ? "group chat" : "channel";
  // The order shown while a drag is being saved; cleared either way, so a
  // failed save falls back to the server's order and a successful one lands
  // on the list the save returned.
  const [pendingOrder, setPendingOrder] = useState<RoomSection[] | null>(null);
  const deleteCancel = useRef<HTMLButtonElement>(null);
  const leaveCancel = useRef<HTMLButtonElement>(null);
  const newSectionInput = useRef<HTMLInputElement>(null);
  // Starring/moving a room re-buckets it into a different group, which
  // unmounts and remounts its row (and overflow trigger) under a new parent
  // list. A ref captured before the action would point at a removed node, so
  // the trigger is found by its stable `data-room-id` after the fact instead.
  const directoryRef = useRef<HTMLElement>(null);
  const groupsRef = useRef<readonly RoomGroup[]>([]);
  // Neither dialog has a DialogTrigger, so Radix has no element to hand focus
  // back to and drops it on <body>. Remember what opened each one.
  const manageTrigger = useRef<HTMLButtonElement>(null);
  const leaveTrigger = useRef<HTMLElement | null>(null);
  const deleteTrigger = useRef<HTMLButtonElement | null>(null);
  const sections = pendingOrder ?? savedSections;
  const drafts = sections.map((section) => ({ ...section, name: nameEdits[section.id] ?? section.name }));
  // One predicate for every write out of this dialog: the backend rejects a
  // blank name, so Add and Delete must not ship someone's half-typed rename
  // along for the ride.
  const blankName = drafts.some((section) => !section.name.trim());
  const trimmedDrafts = () => drafts.map((section) => ({ ...section, name: section.name.trim() }));
  const blankHintId = `${id}-section-blank`;
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );
  const sectionName = (sectionId: string | number) => sections.find((section) => section.id === String(sectionId))?.name ?? "Section";
  // The row `savePrefs` is acting on, so one in-flight star doesn't disable
  // every other row's trigger. Only read while `savePrefs.pending`, so a
  // stale value after the action settles is inert.
  const [actingRoomId, setActingRoomId] = useState<string | null>(null);
  // Same rule as the room panel: a shared room is never added to; a channel takes its owner or an admin; a
  // direct message or group chat, anyone (adding to a DM turns it into a group chat in place).
  const canAddPeople = (room: DirectoryRoom) => !room.shared && (room.kind === "dm" || room.kind === "group-dm" || (room.kind === "channel"
    && room.members.some((member) => member.kind === "human" && member.id === currentUserId && (member.role === "owner" || member.role === "admin"))));
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
  // Muted rooms are unread but never a jump destination -- same predicate
  // the Alt+Shift+Arrow shortcut uses, so the chevron cannot disagree with it.
  const unreadRooms = rooms.filter(isUnreadTarget);
  // A drafted room can be absent from `rooms` (left, or directory still
  // loading) — gate the section on this filtered list, not on
  // draftRoomIds.length, or the heading renders over an empty list.
  const draftedRooms = rooms.filter((room) => draftRoomIds.includes(room.roomId));
  // Slack's Direct messages section is the roster, not the list of rooms that
  // happen to exist: every person and agent gets a row, and a click opens (or
  // creates) the 1:1 DM. Group chats stay as rooms.
  const dmCounterpart = (room: DirectoryRoom) => room.kind === "dm" && room.participants.length === 1
    ? `${room.participants[0].kind}:${room.participants[0].id}` : null;
  const withDm = new Set(rooms.map(dmCounterpart));
  // Super-agent: the roster lists people and the one front agent; specialists are labels on threads, not
  // DM partners (they, and any DM with one, live on the Team page). Off, or while the front agent is still
  // unknown (/agents not answered yet, or no "ceo" role in the roster): every agent, as today.
  const oneVoice = superAgent && !!ceoAgentId;
  const roster = participants.filter((participant) => !withDm.has(`${participant.kind}:${participant.id}`)
    && (!oneVoice || participant.kind !== "agent" || participant.id === ceoAgentId));
  const groups = groupRooms(rooms, sections, { sort: prefs.sort, nameOf: (room) => roomDisplayName(room, room.participants),
    alwaysShow: roster.length > 0 ? ["direct"] : [], currentRoomId,
    ...(oneVoice ? { frontAgentId: ceoAgentId } : {}) });
  // `focusRoomTrigger` can run long after the render that captured it (it's
  // attached to a promise settling), so it must read the CURRENT groups, not
  // whatever `groups` was closed over at click time -- a plain ref, kept in
  // sync every render, sidesteps that staleness without an effect.
  groupsRef.current = groups;

  // The delete confirm replaces the dialog's controls in place, so focus has
  // to follow it — and land on Cancel, never on the destructive button. On the
  // way back it returns to the Delete control that opened it, or to the new
  // section field when that row is gone because the delete went through.
  useEffect(() => {
    if (deleting) { deleteCancel.current?.focus(); return; }
    const opener = deleteTrigger.current;
    deleteTrigger.current = null;
    if (opener) (opener.isConnected ? opener : newSectionInput.current)?.focus();
  }, [deleting]);

  // Section ids are UUIDs — a keyboard drag must never read one aloud.
  const position = (sectionId: string | number) => `position ${sections.findIndex((section) => section.id === String(sectionId)) + 1} of ${sections.length}`;
  const announcements: Announcements = {
    onDragStart: ({ active }) => `Picked up ${sectionName(active.id)}.`,
    onDragOver: ({ active, over }) => over ? `${sectionName(active.id)} is over ${position(over.id)}.` : undefined,
    onDragEnd: ({ active, over }) => over ? `${sectionName(active.id)} was moved to ${position(over.id)}.` : `${sectionName(active.id)} was dropped.`,
    onDragCancel: ({ active }) => `Reordering ${sectionName(active.id)} was cancelled.`,
  };

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

  function focusRoomTrigger(roomId: string) {
    // Room ids are opaque server-issued tokens, never viewer-authored text,
    // so a raw attribute-selector match is safe here.
    const trigger = directoryRef.current?.querySelector<HTMLButtonElement>(`[data-room-id="${roomId}"]`);
    if (trigger) { trigger.focus(); return; }
    // The room's destination group can be collapsed (or not the open room),
    // in which case its row never rendered at all and there is no trigger to
    // find -- land on that group's header toggle instead, the nearest
    // rendered control, rather than losing focus to <body>.
    const group = groupsRef.current.find((candidate) => candidate.rooms.some((room) => room.roomId === roomId));
    if (group) directoryRef.current?.querySelector<HTMLButtonElement>(`[data-group-id="${group.id}"]`)?.focus();
  }

  function setRoomPrefs(roomId: string, next: MemberPrefsRequest, fallback: string) {
    // Mirrors markRoomUnread's guard: without it, reopening the row menu
    // while this room's own save is still in flight fires a second
    // concurrent onSetPrefs call.
    if (!onSetPrefs || (savePrefs.pending && actingRoomId === roomId)) return;
    setActingRoomId(roomId);
    void savePrefs.run(() => onSetPrefs(roomId, next), fallback).then(() => focusRoomTrigger(roomId));
  }

  const updatePrefs = (patch: Partial<SidebarPrefs>) => {
    const next = { ...prefs, ...patch };
    setPrefs(next);
    writeSidebarPrefs(ownerScopeId, next);
  };

  function toggleCollapsed(groupId: string) {
    updatePrefs({ collapsed: prefs.collapsed.includes(groupId) ? prefs.collapsed.filter((id) => id !== groupId) : [...prefs.collapsed, groupId] });
  }

  // Shared by the Sessions group and every room group below: collapse
  // button, aria wiring, chevron, label, and an optional trailing badge.
  function groupHeader({ id: groupId, label, listId, collapsed, grip, badge }: {
    id: string; label: string; listId: string; collapsed: boolean; grip?: ReactNode; badge?: ReactNode;
  }) {
    return <div className="room-directory-group-header">
      {grip}
      <Button variant="ghost" size="icon-sm" aria-expanded={!collapsed} aria-controls={listId} data-group-id={groupId}
        aria-label={`${collapsed ? "Expand" : "Collapse"} ${label}`}
        onClick={() => toggleCollapsed(groupId)}>
        {collapsed ? <ChevronRight aria-hidden="true" /> : <ChevronDown aria-hidden="true" />}
      </Button>
      <h3 className="room-directory-group">{label}</h3>
      {badge}
    </div>;
  }

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
      focusRoomTrigger(roomId);
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
        listenMode: participant.kind === "agent" ? "active" : null,
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

  const sessionsSection = (onSelectSession || sessionRows.length > 0) && (() => {
    const collapsed = prefs.collapsed.includes("sessions");
    const listId = `${id}-group-sessions`;
    const query = sessionFilter.trim();
    // Ended sessions stay until archived (owner decision 2026-10-27), as on the phone and in both vendor apps.
    const { groups, archived, hidden, listed } = sessionRowGroups(sessionRows, { groupBy: prefs.sessionGroupBy, show: prefs.sessionShow, query, marks: sessionMarks });
    // Live rows the viewer has not archived, whatever Show or the filter hides.
    const live = sessionRowGroups(sessionRows, { groupBy: "person", show: "all", query: "", marks: sessionMarks }).groups.flatMap((group) => group.entries).filter(({ row }) => !sessionRowGone(row)).length;
    const waiting = ownWaitingCount(sessionRows.map((entry) => entry.row));
    const pinned = (key: string) => sessionMarks.pinned.includes(key);
    const row = (entry: SidebarRow, isArchived: boolean) => {
      const key = sessionRowKey(entry.row);
      const sessionId = entry.row.backend?.sessionId;
      return <SessionListRow key={key} entry={entry} current={key === currentSessionId} onSelect={onSelectSession} onRename={onRenameSession} oneLine
        onShare={onShareSession && (() => onShareSession(entry.row))}
        marks={{
          pinned: pinned(key), archived: isArchived,
          onTogglePin: () => updateSessionMarks({ ...sessionMarks, pinned: pinned(key) ? sessionMarks.pinned.filter((other) => other !== key) : [...sessionMarks.pinned, key] }),
          // Archiving unpins: a pinned row hidden in Archived would come back pinned on its next output.
          onToggleArchive: () => {
            updateSessionMarks(isArchived ? { ...sessionMarks, archived: Object.fromEntries(Object.entries(sessionMarks.archived).filter(([other]) => other !== key)) }
              : { pinned: sessionMarks.pinned.filter((other) => other !== key), archived: { ...sessionMarks.archived, [key]: entry.row.backend?.latestEventSeq ?? 0 } });
          },
          onMarkUnread: (unread) => { if (sessionId) onMarkSessionUnread?.(sessionId, unread); },
        }} />;
    };
    const toggle = (list: string[], item: string) => (list.includes(item) ? list.filter((other) => other !== item) : [...list, item]);
    const headed = groups.length > 1 || groups[0]?.id === NEEDS_YOU_GROUP;
    const renderSessionGroup = (group: (typeof groups)[number]) => {
      const needs = group.id === NEEDS_YOU_GROUP;
      const key = `sessions:${group.id}`;
      const groupCollapsed = headed && !needs && !query && prefs.collapsed.includes(key);
      const expanded = prefs.sessionsExpanded.includes(group.id);
      const listKey = `${id}-sessions-${groups.indexOf(group)}`;
      const shown = needs ? group.entries : foldedEntries(group.entries, {
        limit: query ? Infinity : groupCollapsed ? 0 : expanded ? Infinity : SESSION_GROUP_FOLD,
        keep: ({ row: kept }) => kept.state === "waiting" || pinned(sessionRowKey(kept)) || sessionRowKey(kept) === currentSessionId,
      });
      const more = group.entries.length - shown.length;
      return <div className="session-group" key={group.id}>
        {headed && (needs
          ? <div className="session-group-header session-group-header--needs"><span>{group.label}</span><span className="session-group-count">{group.entries.length}</span></div>
          : <button type="button" className="session-group-header" aria-expanded={!groupCollapsed} aria-controls={listKey}
            onClick={() => updatePrefs({ collapsed: toggle(prefs.collapsed, key) })}>
            {groupCollapsed ? <ChevronRight aria-hidden="true" /> : <ChevronDown aria-hidden="true" />}
            <span className="truncate">{group.label}</span><span className="session-group-count">{group.entries.length}</span>
          </button>)}
        <ul id={listKey} aria-label={group.label}>{shown.map((entry) => row(entry, false))}</ul>
        {!needs && !groupCollapsed && !query && (more > 0
          ? <button type="button" className="session-group-more" aria-expanded={false} aria-controls={listKey}
            onClick={() => updatePrefs({ sessionsExpanded: toggle(prefs.sessionsExpanded, group.id) })}>… {more} more</button>
          : expanded && group.entries.length > SESSION_GROUP_FOLD && <button type="button" className="session-group-more" aria-expanded aria-controls={listKey}
            onClick={() => updatePrefs({ sessionsExpanded: toggle(prefs.sessionsExpanded, group.id) })}>Show fewer</button>)}
      </div>;
    };
    return <section aria-label="Sessions">
      {groupHeader({
        id: "sessions", label: "Sessions", listId, collapsed,
        badge: <>
          {collapsed && waiting > 0 && <span className="room-count session-count--waiting" aria-label={`${waiting} waiting on you`}>{waiting}</span>}
          {collapsed && live > 0 && <span className="room-count" aria-label={`${live} live session${live === 1 ? "" : "s"}`}>{live}</span>}
          <SessionSearch />
          {sessionRows.length > 0 && <DropdownMenu>
            <DropdownMenuTrigger asChild><Button variant="ghost" size="icon-sm" aria-label="Session view"><SlidersHorizontal aria-hidden="true" /></Button></DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuLabel>Group by</DropdownMenuLabel>
              <DropdownMenuRadioGroup value={prefs.sessionGroupBy} onValueChange={(value) => updatePrefs({ sessionGroupBy: value as SessionGroupBy })}>
                <DropdownMenuRadioItem value="person">Person</DropdownMenuRadioItem>
                <DropdownMenuRadioItem value="state">State</DropdownMenuRadioItem>
                <DropdownMenuRadioItem value="repo">Repo or folder</DropdownMenuRadioItem>
              </DropdownMenuRadioGroup>
              <DropdownMenuSeparator />
              <DropdownMenuLabel>Show</DropdownMenuLabel>
              <DropdownMenuRadioGroup value={prefs.sessionShow} onValueChange={(value) => updatePrefs({ sessionShow: value as SessionShow })}>
                <DropdownMenuRadioItem value="all">All</DropdownMenuRadioItem>
                <DropdownMenuRadioItem value="mine">Mine</DropdownMenuRadioItem>
                <DropdownMenuRadioItem value="live">Live</DropdownMenuRadioItem>
              </DropdownMenuRadioGroup>
            </DropdownMenuContent>
          </DropdownMenu>}
        </>,
      })}
      {!collapsed && <div id={listId}>
        {groups.length === 0 && !hidden && !query
          ? <div className="room-directory-session-empty"><p className="text-caption text-muted-foreground">No one is sharing a session.</p><StartSessionDialog /></div>
          : <>
            {(listed > SESSION_GROUP_FOLD || sessionFilter) && <input type="search" className="session-filter" aria-label="Filter sessions" placeholder="Filter sessions" value={sessionFilter}
              onChange={(event) => setSessionFilter(event.target.value)} onKeyDown={(event) => { if (event.key === "Escape" && sessionFilter) { event.stopPropagation(); setSessionFilter(""); } }} />}
            {groups.map(renderSessionGroup)}
            {query && groups.length === 0 && <p role="status" className="px-2 text-caption text-muted-foreground">No sessions match &quot;{query}&quot;. <button type="button" className="underline" onClick={() => setSessionFilter("")}>Clear</button></p>}
            {hidden > 0 && <button type="button" className="room-directory-subgroup" onClick={() => updatePrefs({ sessionShow: "all" })}>
              {hidden} {prefs.sessionShow === "live" ? "ended or offline hidden" : "others hidden"} · Show all
            </button>}
          </>}
        {archived.length > 0 && <>
          <button type="button" className="room-directory-subgroup" aria-expanded={archivedOpen} onClick={() => setArchivedOpen((open) => !open)}>
            {archivedOpen ? <ChevronDown aria-hidden="true" /> : <ChevronRight aria-hidden="true" />}Archived<span className="text-muted-foreground">{archived.length}</span>
          </button>
          {archivedOpen && <ul aria-label="Archived sessions">{archived.map((entry) => row(entry, true))}</ul>}
        </>}
        {onOpenAllSessions && listed > 0 && <button type="button" className="room-directory-subgroup"
          aria-current={allSessionsOpen ? "page" : undefined} onClick={onOpenAllSessions}>
          <History aria-hidden="true" />All sessions<span className="text-muted-foreground">{listed}</span>
        </button>}
      </div>}
    </section>;
  })();
  const external = groups.find((group) => group.id === "external");
  const moreHoldsCurrent = Boolean(currentSessionId) || Boolean(external?.rooms.some((room) => room.roomId === currentRoomId));
  // What More hides: shared-channel unreads and sessions waiting on the viewer. Its heading carries the sum, and it
  // opens itself while there is any, so nothing new sits out of sight.
  const moreCount = (external?.rooms ?? []).reduce((n, room) => n + (room.muted ? 0 : room.unreadCount), 0)
    + ownWaitingCount(sessionRows.map((entry) => entry.row));
  // Closed, More still says how much is inside, so it never reads as an empty heading.
  const moreItems = sessionRows.length + (external?.rooms.length ?? 0);
  const renderGroup = (group: RoomGroup) => {
    // Archived starts collapsed, so for it alone a listed id means "expanded".
    const collapsed = prefs.collapsed.includes(group.id) !== (group.id === "archived");
    // Collapsing hides rooms, never activity or your place: the header keeps
    // the group's unread count, and the room you are reading stays on screen
    // so `aria-current` survives a ⌘K / next-unread / permalink jump into a
    // collapsed group.
    const unread = group.rooms.reduce((total, room) => total + (room.muted ? 0 : room.unreadCount), 0);
    const visible = collapsed ? group.rooms.filter((room) => room.roomId === currentRoomId) : group.rooms;
    const listId = `${id}-group-${group.id}`;
    const body = (grip?: ReactNode) => {
      const rows: Array<{ key: string; label: string; room?: DirectoryRoom; participant?: DirectoryParticipant }> = [
        ...visible.map((room) => ({ key: room.roomId, label: roomDisplayName(room, room.participants), room })),
        ...(group.id === "direct" && !collapsed ? roster.map((participant) => ({ key: `${participant.kind}:${participant.id}`, label: participant.displayName, participant })) : []),
      ];
      if (group.id === "direct") {
        const unreadOf = (row: typeof rows[number]) => row.room && !row.room.muted ? row.room.unreadCount : 0;
        rows.sort((left, right) => prefs.sort === "unread" && unreadOf(left) !== unreadOf(right)
          ? unreadOf(right) - unreadOf(left) : left.label.localeCompare(right.label));
        // The CEO's DM leads Team, whether it is a room yet or still a roster row.
        const ceo = rows.findIndex((row) => row.participant ? row.participant.kind === "agent" && row.participant.id === ceoAgentId
          : row.room?.kind === "dm" && row.room.participants.length === 1 && row.room.participants[0].kind === "agent" && row.room.participants[0].id === ceoAgentId);
        if (ceoAgentId && ceo > 0) rows.unshift(...rows.splice(ceo, 1));
      }
      return <>
      {groupHeader({
        id: group.id, label: group.label, listId, collapsed, grip,
        badge: collapsed && unread > 0 && <span className="room-count" aria-label={`${unread} unread messages in ${group.label}`}>{unread}</span>,
      })}
      <ul id={listId}>{rows.map(({ key, label, room, participant }) => {
        // A roster row is someone with no DM yet. It carries no overflow menu
        // (star, mute, mark-unread and copy-link all need a room), but it
        // must still reserve that control's width -- see
        // .room-directory-row--roster. Without it the label and the "Agent"
        // tag sit ~36px further right than every real row's, and jump left
        // the instant a first click creates the room.
        if (!room) return <li key={key} className="room-directory-row room-directory-row--roster">
          <button type="button" className="room-directory-entry" aria-busy={openingParticipant === key}
            disabled={openingParticipant === key} onClick={() => onSelectParticipant(participant!)}>
            <ParticipantAvatar participant={participant!} size="xs" />
            <span className="min-w-0 flex-1"><span className="block truncate" title={label}>{label}</span></span>
            {participant!.kind === "agent" && <span className="room-directory-tag">Agent</span>}
          </button>
        </li>;
        return <RoomDirectoryRow key={room.roomId} room={room} label={label} sections={sections}
          active={room.roomId === currentRoomId} draft={draftRoomIds.includes(room.roomId)}
          busy={markingUnread === room.roomId || (savePrefs.pending && actingRoomId === room.roomId)}
          onSelect={() => onSelectRoom(room.roomId)}
          onToggleStar={onSetPrefs && (() => setRoomPrefs(room.roomId, { starred: !room.starred }, `Could not ${room.starred ? "unstar" : "star"} ${label}.`))}
          onToggleMute={onSetPrefs && (() => setRoomPrefs(room.roomId, { muted: !room.muted }, `Could not ${room.muted ? "unmute" : "mute"} ${label}.`))}
          onMarkUnread={onMarkUnread && (() => void markRoomUnread(room.roomId, label))}
          onMoveToSection={onSetPrefs && sections.length > 0
            ? (sectionId) => setRoomPrefs(room.roomId, { sectionId }, `Could not move ${label}.`) : undefined}
          onLeave={onLeaveRoom && (room.kind === "channel" || room.kind === "group-dm") && !room.shared && !soleChannelOwner(room, currentUserId) ? (trigger) => { leave.reset(); leaveTrigger.current = trigger; setLeaving(room); } : undefined}
          onCopyLink={() => void copyText(roomPermalink(room.roomId))}
          onAddPeople={onAddPeople && canAddPeople(room) ? () => onAddPeople(room.roomId) : undefined} />;
      })}</ul>
    </>;
    };
    // No grip at all when the caller cannot save an order — a drag handle
    // that silently does nothing is worse than no handle.
    return group.custom && onSaveSections
      ? <SortableSection key={group.id} id={group.id} label={group.label}>{body}</SortableSection>
      : <section key={group.id} aria-label={group.label}>{body()}</section>;
  };

  return <Dialog open={creating} onOpenChange={(open) => { if (!creationBusy) setCreating(open); }}><nav ref={directoryRef} className="room-directory" aria-label="Conversations">
    <div className="room-directory-heading"><h2 className="font-semibold">Conversations</h2>
      <div className="room-directory-heading-actions">
        <DropdownMenu>
          <DropdownMenuTrigger asChild><Button ref={manageTrigger} variant="ghost" size="icon-sm" aria-label="Sidebar options"><MoreHorizontal aria-hidden="true" /></Button></DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuLabel>Sort</DropdownMenuLabel>
            <DropdownMenuRadioGroup value={prefs.sort} onValueChange={(value) => updatePrefs({ sort: value as SidebarSort })}>
              <DropdownMenuRadioItem value="recent">Recent activity</DropdownMenuRadioItem>
              <DropdownMenuRadioItem value="name">A–Z</DropdownMenuRadioItem>
              <DropdownMenuRadioItem value="unread">Unread first</DropdownMenuRadioItem>
            </DropdownMenuRadioGroup>
            <DropdownMenuSeparator />
            <DropdownMenuItem disabled={unreadRooms.length === 0} onSelect={selectNextUnread}>Jump to next unread</DropdownMenuItem>
            {onSaveSections && <DropdownMenuItem onSelect={openManageSections}>Manage sections</DropdownMenuItem>}
          </DropdownMenuContent>
        </DropdownMenu>
        {headerActions}
        {onCreate && <DialogTrigger asChild><Button variant="ghost" size="icon-sm" aria-label="New conversation" disabled={creationBusy}><Plus aria-hidden="true" /></Button></DialogTrigger>}
      </div>
    </div>
    {creationError && !creating && <p role="alert" className="px-2 text-ui text-err">{creationError}</p>}
    {unreadError && <p role="alert" className="px-2 text-ui text-err">Could not mark {unreadError.label} unread. <Button variant="outline" disabled={markingUnread !== null}
      onClick={() => void markRoomUnread(unreadError.roomId, unreadError.label)}>Try again</Button></p>}
    {partialError && <div role="alert" className="px-2 text-ui text-err">Some conversation details could not be loaded. {onRetry && <Button variant="outline" onClick={onRetry}>Try again</Button>}</div>}
    {savePrefs.error && <p role="alert" className="px-2 text-ui text-err">{savePrefs.error}</p>}
    {saveSections.error && !managing && <p role="alert" className="px-2 text-ui text-err">{saveSections.error}</p>}
    {groups.length === 0 && <p className="text-ui text-muted-foreground px-2">Create a channel, or start a conversation with a person or agent.</p>}
    <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={reorderSections} accessibility={{ announcements }}>
    <SortableContext items={sections.map((section) => section.id)} strategy={verticalListSortingStrategy}>
    {groups.filter((group) => !TAIL_GROUPS.has(group.id)).map(renderGroup)}
    {/* Sessions and External connections fold under More (closed by default); it opens itself while the
        session or shared channel on screen is inside, or while anything in it is unread or waiting. */}
    {(sessionsSection || external) && <details className="room-directory-more" open={moreHoldsCurrent || moreCount > 0 || undefined}>
      <summary className="room-directory-group"
        aria-label={moreCount > 0 ? `More, ${moreCount} unread or waiting` : moreItems > 0 ? `More, ${moreItems} inside` : "More"}>
        <ChevronRight aria-hidden="true" className="room-directory-more-chevron" />More
        {moreCount > 0
          ? <span className="room-count" aria-hidden="true">{moreCount}</span>
          : moreItems > 0 && <span className="text-muted-foreground" aria-hidden="true">{moreItems}</span>}</summary>
      {sessionsSection}
      {external && renderGroup(external)}
    </details>}
    {groups.filter((group) => group.id === "archived").map(renderGroup)}
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
    <button type="button" className="room-directory-entry" onClick={() => { openInviteDialog(); onNavigate?.(); }}><Plus size={16} aria-hidden="true" />Invite someone</button>
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
      <p className="text-ui text-muted-foreground">Only selected workspace members can read this conversation.</p>
      <div className="room-picker">
        <p className="text-ui font-semibold">People and agents · {selected.size} selected</p>
        <label htmlFor={`${id}-search`} className="sr-only">Find people and agents</label>
        <input id={`${id}-search`} className="room-input" type="search" placeholder="Find a person or agent…" value={query} onChange={(event) => setQuery(event.target.value)} />
        <div className="room-picker-list">{visibleParticipants.map((participant) => {
          const key = `${participant.kind}:${participant.id}`;
          const selection = selected.get(key);
          const content = <>
            <input aria-label={`Select ${participant.displayName}`} type="checkbox" checked={!!selection} disabled={creationBusy || (!selection && selected.size >= 31)} onChange={(event) => toggleParticipant(participant, event.target.checked)} />
            <Identity name={participant.displayName} avatarOnly size="sm" />
            <span className="min-w-0 flex-1"><span className="block truncate" title={participant.displayName}>{participant.displayName}</span>
              <span className="text-ui text-muted-foreground">{participant.kind === "agent" ? participant.mentionHandle ? `@${participant.mentionHandle}` : "Agent" : "Person"}</span></span>
            {selection?.kind === "agent" && <label className="sr-only" htmlFor={`${id}-${participant.id}-listen`}>Response for {participant.displayName}</label>}
            {selection?.kind === "agent" && <select id={`${id}-${participant.id}-listen`} className="room-listen-mode" value={selection.listenMode ?? "mentions"} disabled={creationBusy}
              onChange={(event) => setListenMode(participant, event.target.value as ListenMode)}>
              {listenModeOptions().map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
            </select>}</>;
          return <div className="room-choice" key={key}>{content}</div>;
        })}</div>
        {visibleParticipants.length === 0 && <p className="text-ui text-muted-foreground">No matching people or agents.</p>}
      </div>
      </div>
      <div className="room-creation-footer">
        {creationError && <p role="alert" className="text-ui text-err">{creationError}</p>}
        {/* Not room-composer-actions: that class forces 28px icon-button squares
            for the message composer's toolbar, which clipped and overlapped these
            full-width text buttons. DialogFooter is the pattern every other dialog
            footer here already uses (Manage sections, Leave, Rename). */}
        <DialogFooter className="gap-2"><Button type="button" variant="ghost" disabled={creationBusy} onClick={() => setCreating(false)}>Cancel</Button>
          <Button type="submit" disabled={!canCreate || creationBusy}>{creationBusy ? "Creating…" : kind === "channel" ? "Create channel" : kind === "group-dm" ? "Start group chat" : "Start direct message"}</Button></DialogFooter>
      </div>
    </form>
    </DialogContent>
    <Dialog open={managing} onOpenChange={(open) => { if (!open && !saveSections.pending) { setManaging(false); saveSections.reset(); } }}>
      <DialogContent className="room-section-dialog max-w-[calc(100%-2rem)] sm:max-w-md max-h-[calc(100dvh-2rem)] overflow-y-auto"
        onCloseAutoFocus={(event) => { if (manageTrigger.current?.isConnected) { event.preventDefault(); manageTrigger.current.focus(); } }}>
        <DialogHeader><DialogTitle>Manage sections</DialogTitle>
          <DialogDescription>Group conversations however you work. Deleting a section returns its conversations to Team, Channels, or External connections.</DialogDescription>
        </DialogHeader>
        {sections.length > 0 && <ul className="grid gap-2">{sections.map((section) => <li key={section.id} className="room-section-edit">
          {/* Both accessible names stay on the SAVED name: a control whose name
              changes under the caret as you type is unusable with a screen reader. */}
          <label className="sr-only" htmlFor={`${id}-section-${section.id}`}>Rename {section.name}</label>
          <input id={`${id}-section-${section.id}`} className="room-input" value={nameEdits[section.id] ?? section.name}
            maxLength={SECTION_NAME_MAX_CHARS} disabled={saveSections.pending || !!deleting}
            aria-invalid={(nameEdits[section.id] ?? section.name).trim() ? undefined : true}
            aria-describedby={(nameEdits[section.id] ?? section.name).trim() ? undefined : blankHintId}
            onChange={(event) => setNameEdits({ ...nameEdits, [section.id]: event.target.value })} />
          <Button type="button" variant="ghost" size="icon" aria-label={`Delete ${section.name}`}
            disabled={saveSections.pending || !!deleting || blankName}
            onClick={(event) => { saveSections.reset(); deleteTrigger.current = event.currentTarget; setDeleting(section); }}><Trash2 aria-hidden="true" /></Button>
        </li>)}</ul>}
        <form className="room-section-edit" onSubmit={async (event) => {
          event.preventDefault();
          const name = newSection.trim();
          if (!name || sections.length >= MAX_SECTIONS) return;
          if (blankName) return;
          if (await persistSections([...trimmedDrafts(), { id: crypto.randomUUID(), name }])) { setNewSection(""); setNameEdits({}); }
        }}>
          <label className="sr-only" htmlFor={`${id}-new-section`}>New section name</label>
          <input ref={newSectionInput} id={`${id}-new-section`} className="room-input" value={newSection} maxLength={SECTION_NAME_MAX_CHARS}
            disabled={saveSections.pending || !!deleting} onChange={(event) => setNewSection(event.target.value)} />
          <Button type="submit" variant="outline" disabled={!newSection.trim() || sections.length >= MAX_SECTIONS || saveSections.pending || !!deleting || blankName}>Add section</Button>
        </form>
        {sections.length >= MAX_SECTIONS && <p className="text-ui text-muted-foreground">You can keep up to {MAX_SECTIONS} sections.</p>}
        {blankName && <p id={blankHintId} className="text-ui text-muted-foreground">Every section needs a name.</p>}
        {saveSections.error && <p role="alert" className="text-ui text-err break-words">{saveSections.error}</p>}
        {deleting && <div role="alert" className="room-section-confirm">
          <p className="text-ui">Delete {deleting.name}? Its conversations move back to Team, Channels, or External connections.</p>
          <DialogFooter className="gap-2">
            <Button ref={deleteCancel} type="button" variant="outline" disabled={saveSections.pending} onClick={() => { setDeleting(null); saveSections.reset(); }}>Cancel</Button>
            <Button type="button" variant="destructive" disabled={saveSections.pending || blankName}
              onClick={async () => { if (await persistSections(trimmedDrafts().filter((section) => section.id !== deleting.id))) { setDeleting(null); setNameEdits({}); } }}>
              {saveSections.pending ? "Deleting…" : "Delete section"}</Button>
          </DialogFooter>
        </div>}
        {!deleting && <DialogFooter className="gap-2">
          <Button type="button" variant="outline" disabled={saveSections.pending} onClick={() => { setNameEdits({}); setManaging(false); saveSections.reset(); }}>Cancel</Button>
          <Button type="button" disabled={saveSections.pending || blankName}
            onClick={async () => { if (await persistSections(trimmedDrafts())) { setNameEdits({}); setManaging(false); } }}>
            {saveSections.pending ? "Saving…" : "Save sections"}</Button>
        </DialogFooter>}
      </DialogContent>
    </Dialog>
    <Dialog open={!!leaving} onOpenChange={(open) => { if (!open && !leave.pending) { setLeaving(null); leave.reset(); } }}>
      <DialogContent className="room-section-dialog max-w-[calc(100%-2rem)] sm:max-w-md"
        onOpenAutoFocus={(event) => { event.preventDefault(); leaveCancel.current?.focus(); }}
        onCloseAutoFocus={(event) => { if (leaveTrigger.current?.isConnected) { event.preventDefault(); leaveTrigger.current.focus(); } }}>
        <DialogHeader><DialogTitle className="break-words">Leave {leaving ? roomDisplayName(leaving, leaving.participants) : ""}?</DialogTitle>
          <DialogDescription>You will stop receiving its messages and lose access to its history.{leaving?.kind === "group-dm" ? "" : " An admin can add you back."}</DialogDescription>
        </DialogHeader>
        {leave.error && <p role="alert" className="text-ui text-err break-words">{leave.error}</p>}
        <DialogFooter className="gap-2">
          <Button ref={leaveCancel} type="button" variant="outline" disabled={leave.pending} onClick={() => { setLeaving(null); leave.reset(); }}>Cancel</Button>
          <Button type="button" variant="destructive" disabled={leave.pending}
            onClick={() => void leave.run(() => onLeaveRoom!(leaving!.roomId), `The ${leavingNoun} could not be left.`, () => setLeaving(null))}>
            {leave.pending ? "Leaving…" : `Leave ${leavingNoun}`}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </nav></Dialog>;
}
