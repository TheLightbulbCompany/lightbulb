"use client";

import { useId, useState } from "react";
import { ChevronDown, ChevronRight, Pencil, Plus, Users } from "lucide-react";
import type { ListenMode, MemberPrefsRequest, RoomKind, RoomSection } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
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
  creationBusy?: boolean;
  creationError?: string;
  onRetry?: () => void;
  draftRoomIds?: readonly string[];
}

export function RoomDirectory(props: RoomDirectoryProps) {
  return <DirectoryContent key={JSON.stringify([props.ownerScopeId, props.currentRoomId])} {...props} />;
}

function DirectoryContent({
  ownerScopeId,
  rooms,
  sections,
  participants,
  state,
  currentRoomId,
  onSelectRoom,
  onSelectParticipant,
  onCreate,
  onMarkUnread,
  onSetPrefs,
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

  function setRoomPrefs(roomId: string, next: MemberPrefsRequest, fallback: string) {
    if (!onSetPrefs) return;
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
        {onCreate && <DialogTrigger asChild><Button variant="ghost" size="icon" aria-label="New conversation" disabled={creationBusy}><Plus aria-hidden="true" /></Button></DialogTrigger>}
      </div>
    </div>
    {unreadError && <p role="alert" className="px-2 text-sm text-err">Could not mark {unreadError.label} unread. <Button variant="outline" disabled={markingUnread !== null}
      onClick={() => void markRoomUnread(unreadError.roomId, unreadError.label)}>Try again</Button></p>}
    {partialError && <div role="alert" className="px-2 text-sm text-err">Some conversation details could not be loaded. {onRetry && <Button variant="outline" onClick={onRetry}>Try again</Button>}</div>}
    {savePrefs.error && <p role="alert" className="px-2 text-sm text-err">{savePrefs.error}</p>}
    {groups.length === 0 && <p className="text-sm text-muted-foreground px-2">Create a channel, or start a conversation with a person or agent.</p>}
    {groups.map((group) => {
      const collapsed = prefs.collapsed.includes(group.id);
      return <section key={group.id} aria-label={group.label}>
        <div className="room-directory-group-header">
          <h3 className="room-directory-group">{group.label}</h3>
          <Button variant="ghost" size="icon-sm" aria-label={`${collapsed ? "Expand" : "Collapse"} ${group.label}`}
            onClick={() => updatePrefs({ collapsed: collapsed ? prefs.collapsed.filter((groupId) => groupId !== group.id) : [...prefs.collapsed, group.id] })}>
            {collapsed ? <ChevronRight aria-hidden="true" /> : <ChevronDown aria-hidden="true" />}
          </Button>
        </div>
        {!collapsed && <ul>{group.rooms.map((room) => {
          const label = roomDisplayName(room, room.participants);
          return <RoomDirectoryRow key={room.roomId} room={room} label={label} sections={sections}
            active={room.roomId === currentRoomId} draft={draftRoomIds.includes(room.roomId)}
            busy={markingUnread === room.roomId || savePrefs.pending}
            onSelect={() => onSelectRoom(room.roomId)}
            onToggleStar={() => setRoomPrefs(room.roomId, { starred: !room.starred }, `Could not ${room.starred ? "unstar" : "star"} ${label}.`)}
            onToggleMute={() => setRoomPrefs(room.roomId, { muted: !room.muted }, `Could not ${room.muted ? "unmute" : "mute"} ${label}.`)}
            onMarkUnread={() => void markRoomUnread(room.roomId, label)}
            onCopyLink={() => void copyText(roomPermalink(room.roomId))} />;
        })}</ul>}
      </section>;
    })}
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
  </nav></Dialog>;
}
