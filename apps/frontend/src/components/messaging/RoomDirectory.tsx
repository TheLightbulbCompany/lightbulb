"use client";

import { useId, useState } from "react";
import { ChevronDown, Hash, MailOpen, MessageCircle, MoreHorizontal, Plus, Users } from "lucide-react";
import type { ListenMode, RoomKind } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Identity } from "@/components/teams/shared/components/Identity";
import { ParticipantAvatar } from "./ParticipantAvatar";
import { roomDisplayName, roomKindLabel, type DirectoryParticipant, type ParticipantSelection, type DirectoryRoom } from "./types";
import "./messaging.css";

export interface ConversationCreation {
  kind: RoomKind;
  name: string | null;
  members: ParticipantSelection[];
}

export interface RoomDirectoryProps {
  ownerScopeId: string;
  rooms: readonly DirectoryRoom[];
  participants: readonly DirectoryParticipant[];
  state: "loading" | "ready" | "error" | "access-removed";
  currentRoomId?: string;
  onSelectRoom: (roomId: string) => void;
  onSelectParticipant: (participant: DirectoryParticipant) => void | Promise<void>;
  onCreate?: (creation: ConversationCreation) => void | Promise<boolean>;
  onMarkUnread?: (roomId: string) => void | Promise<void>;
  creationBusy?: boolean;
  creationError?: string;
  onRetry?: () => void;
}

export function RoomDirectory(props: RoomDirectoryProps) {
  return <DirectoryContent key={JSON.stringify([props.ownerScopeId, props.currentRoomId])} {...props} />;
}

function DirectoryContent({
  rooms,
  participants,
  state,
  currentRoomId,
  onSelectRoom,
  onSelectParticipant,
  onCreate,
  onMarkUnread,
  creationBusy = false,
  creationError,
  onRetry,
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
  const validName = kind !== "channel" || (name.trim().length >= 1 && name.trim().length <= 80);
  const validCount = kind === "dm" ? selected.size === 1 : kind === "group-dm" ? selected.size >= 2 : true;
  const canCreate = !!onCreate && validName && validCount && selected.size <= 31;
  const visibleParticipants = participants.filter((participant) => {
    const needle = query.toLocaleLowerCase();
    return participant.displayName.toLocaleLowerCase().includes(needle) || participant.mentionHandle?.includes(needle);
  });
  const partialError = state === "error" && rooms.length > 0;
  const unreadRooms = rooms.filter((room) => room.unreadCount > 0);

  function selectNextUnread() {
    if (unreadRooms.length === 0) return;
    const currentIndex = rooms.findIndex((room) => room.roomId === currentRoomId);
    const next = [...rooms.slice(currentIndex + 1), ...rooms.slice(0, currentIndex + 1)]
      .find((room) => room.unreadCount > 0);
    if (next) onSelectRoom(next.roomId);
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
        <Button variant="ghost" size="icon" aria-label="Next unread conversation" disabled={unreadRooms.length === 0} onClick={selectNextUnread}><ChevronDown aria-hidden="true" /></Button>
        {onCreate && <DialogTrigger asChild><Button variant="ghost" size="icon" aria-label="New conversation" disabled={creationBusy}><Plus aria-hidden="true" /></Button></DialogTrigger>}
      </div>
    </div>
    {unreadError && <p role="alert" className="px-2 text-sm text-err">Could not mark {unreadError.label} unread. <Button variant="outline" disabled={markingUnread !== null}
      onClick={() => void markRoomUnread(unreadError.roomId, unreadError.label)}>Try again</Button></p>}
    {partialError && <div role="alert" className="px-2 text-sm text-err">Some conversation details could not be loaded. {onRetry && <Button variant="outline" onClick={onRetry}>Try again</Button>}</div>}
    {(["Channels", "Group chats", "Direct messages"] as const).map((group) => {
      const kind = group === "Channels" ? "channel" : group === "Group chats" ? "group-dm" : "dm";
      const entries = rooms.filter((room) => room.kind === kind);
      return <section key={group} aria-label={group}>
        <h3 className="room-directory-group">{group}</h3>
        {entries.length === 0 && <p className="text-sm text-muted-foreground px-2">{
          group === "Channels" ? "Create a channel for shared work."
            : group === "Group chats" ? "Start a conversation with two or more people or agents."
              : "Start a conversation with a person or agent."
        }</p>}
        <ul>{entries.map((room) => {
          const Icon = room.kind === "channel" ? Hash : room.kind === "group-dm" ? Users : MessageCircle;
          const label = roomDisplayName(room, room.participants);
          return <li key={room.roomId} className="room-directory-row"><button type="button" className={`room-directory-entry ${room.unreadCount > 0 ? "font-semibold" : ""}`}
            aria-current={room.roomId === currentRoomId ? "page" : undefined} onClick={() => onSelectRoom(room.roomId)}>
            {room.kind === "dm" && room.participants.length === 1
              ? <ParticipantAvatar participant={room.participants[0]} size="sm" />
              : <Icon size={16} aria-hidden="true" />}
            <span className="min-w-0 flex-1"><span className="block truncate" title={label}>{label}</span><span className="sr-only">{roomKindLabel[room.kind]}</span></span>
            {room.unreadCount > 0 && <span className="room-count" aria-label={`${room.unreadCount} unread messages`}>{room.unreadCount}</span>}
          </button>{onMarkUnread && <DropdownMenu><DropdownMenuTrigger asChild>
            <Button variant="ghost" size="icon" className="room-directory-actions" aria-label={`Conversation actions for ${label}`} disabled={markingUnread === room.roomId}><MoreHorizontal aria-hidden="true" /></Button>
          </DropdownMenuTrigger><DropdownMenuContent align="end"><DropdownMenuItem disabled={markingUnread !== null} onSelect={() => void markRoomUnread(room.roomId, label)}>
            <MailOpen aria-hidden="true" />{markingUnread === room.roomId ? "Marking unread…" : "Mark unread"}
          </DropdownMenuItem></DropdownMenuContent></DropdownMenu>}</li>;
        })}</ul>
      </section>;
    })}
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
        const created = await onCreate?.({ kind, name: kind === "channel" ? name.trim() : null, members: [...selected.values()] });
        if (created) { setCreating(false); setName(""); setSelected(new Map()); setQuery(""); setKind("channel"); }
      }}>
      <div className="room-creation-header">
        <DialogTitle>{kind === "channel" ? "Create a channel" : "New message"}</DialogTitle>
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
      {kind === "channel" && <><label htmlFor={`${id}-name`}>Channel name</label>
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
            {selection?.kind === "agent" && <label className="sr-only" htmlFor={`${id}-${participant.id}-listen`}>Listen mode for {participant.displayName}</label>}
            {selection?.kind === "agent" && <select id={`${id}-${participant.id}-listen`} className="room-listen-mode" value={selection.listenMode ?? "mentions"} disabled={creationBusy}
              onChange={(event) => setListenMode(participant, event.target.value as ListenMode)}>
              <option value="active">Active</option><option value="mentions">Mentions only</option><option value="observe">Observe</option>
            </select>}</>;
          return <div className="room-choice" key={key}>{content}</div>;
        })}</div>
        {visibleParticipants.length === 0 && <p className="text-sm text-muted-foreground">No matching people or agents.</p>}
      </div>
      </div>
      <div className="room-creation-footer">
        {creationError && <p role="alert" className="text-sm text-err">{creationError}</p>}
        <div className="room-composer-actions"><Button type="button" variant="ghost" disabled={creationBusy} onClick={() => setCreating(false)}>Cancel</Button>
          <Button type="submit" disabled={!canCreate || creationBusy}>{creationBusy ? "Creating…" : kind === "channel" ? "Create channel" : "Start message"}</Button></div>
      </div>
    </form>
    </DialogContent>
  </nav></Dialog>;
}
