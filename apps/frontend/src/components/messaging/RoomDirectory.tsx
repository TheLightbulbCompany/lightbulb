"use client";

import { useId, useState } from "react";
import { Hash, LockKeyhole, MessageCircle, Plus, Users } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Identity } from "@/components/teams/shared/components/Identity";
import { roomKindLabel, type RoomParticipant, type RoomSummary } from "./types";
import "./messaging.css";

type CreationKind = "public" | "private" | "conversation";
export interface ConversationCreation {
  kind: CreationKind;
  name?: string;
  participantRefs: string[];
}
export interface RoomDirectoryProps {
  ownerScopeId: string;
  rooms: readonly RoomSummary[];
  participants: readonly RoomParticipant[];
  state: "loading" | "ready" | "error" | "access-removed";
  currentRoomId?: string;
  onSelectRoom: (roomId: string) => void;
  permittedCreation?: readonly CreationKind[];
  onCreate?: (creation: ConversationCreation) => void;
  creationBusy?: boolean;
  creationError?: string;
  onRetry?: () => void;
  /** Authoritative existing DM participants; adding starts a new history. */
  newConversationFrom?: { roomId: string; participantRefs: readonly string[] };
  onNewConversation?: (creation: { participantRefs: string[] }) => void;
}

/** Remount local search/creation state when scope or selected conversation changes. */
export function RoomDirectory(props: RoomDirectoryProps) {
  return <DirectoryContent key={JSON.stringify([props.ownerScopeId, props.currentRoomId])} {...props} />;
}

function DirectoryContent({ rooms, participants, state, currentRoomId, onSelectRoom,
  permittedCreation = [], onCreate, creationBusy = false, creationError, onRetry,
  newConversationFrom, onNewConversation }: RoomDirectoryProps) {
  const id = useId();
  const [mode, setMode] = useState<"closed" | "create" | "extend">("closed");
  const [kind, setKind] = useState<CreationKind>(permittedCreation[0] ?? "conversation");
  const [name, setName] = useState("");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [directoryOpen, setDirectoryOpen] = useState(false);
  const selectedRoom = rooms.find(room => room.id === currentRoomId);
  const canExtend = onNewConversation && newConversationFrom?.roomId === currentRoomId &&
    (selectedRoom?.kind === "dm" || selectedRoom?.kind === "group");
  const authorizedRefs = new Set(participants.map(person => person.ref));
  const validSelection = selected.every(ref => authorizedRefs.has(ref));
  const hasNewParticipant = selected.some(ref => !newConversationFrom?.participantRefs.includes(ref));
  const validName = name.trim().length >= 1 && name.trim().length <= 80;
  const canCreate = mode === "extend" ? !!canExtend && hasNewParticipant && validSelection
    : permittedCreation.includes(kind) && validSelection && (kind === "conversation" ? selected.length > 0 : validName);
  const visibleParticipants = participants.filter(person => person.name.toLocaleLowerCase().includes(query.toLocaleLowerCase()));

  if (state !== "ready") return <nav className="room-directory" aria-label="Conversations">
    <h2 className="font-semibold">Conversations</h2>
    <p role="status">{state === "loading" ? "Loading conversations…" : state === "access-removed" ? "Access removed" : "Conversations could not be loaded."}</p>
    {state === "loading" && <div aria-hidden="true" className="room-skeleton"><span /><span /><span /></div>}
    {state === "error" && onRetry && <Button variant="outline" onClick={onRetry}>Try again</Button>}
  </nav>;

  return <nav className="room-directory" aria-label="Conversations">
    <div className="room-directory-heading"><h2 className="font-semibold">Conversations</h2>
      {onCreate && permittedCreation.length > 0 && <Button variant="ghost" size="icon" aria-label="New conversation"
        disabled={creationBusy} onClick={() => { setMode("create"); setSelected([]); setQuery(""); setKind(permittedCreation[0]); }}><Plus aria-hidden="true" /></Button>}
    </div>
    {(["Rooms", "Direct messages"] as const).map(group => {
      const entries = rooms.filter(room => group === "Rooms" ? room.kind === "public" || room.kind === "private" : room.kind === "dm" || room.kind === "group");
      return <section key={group} aria-label={group}>
        <h3 className="room-directory-group">{group}</h3>
        {entries.length === 0 && <p className="text-sm text-muted-foreground px-2">{group === "Rooms" ? "Your rooms will appear here." : "Start a conversation with a person or agent."}</p>}
        <ul>{entries.map(room => {
          const Icon = room.kind === "private" ? LockKeyhole : room.kind === "public" ? Hash : room.kind === "group" ? Users : MessageCircle;
          return <li key={room.id}><button type="button" className={`room-directory-entry ${room.unread ? "font-semibold" : ""}`}
            aria-current={room.id === currentRoomId ? "page" : undefined} onClick={() => onSelectRoom(room.id)}>
            <Icon size={16} aria-hidden="true" />
            <span className="min-w-0 flex-1"><span className="block truncate" title={room.name}>{room.name}</span><span className="sr-only">{roomKindLabel[room.kind]}</span></span>
            {room.unread && <span aria-label="Unread" className="room-unread" />}
            {Number.isSafeInteger(room.mentionCount) && room.mentionCount! > 0 && <span className="room-count" aria-label={`${room.mentionCount} mentions`}>{room.mentionCount}</span>}
            {Number.isSafeInteger(room.approvalCount) && room.approvalCount! > 0 && <span className="room-count text-warn" aria-label={`${room.approvalCount} approvals`}>{room.approvalCount}</span>}
          </button></li>;
        })}</ul>
      </section>;
    })}
    {canExtend && newConversationFrom && <Button variant="ghost" disabled={creationBusy} onClick={() => { setMode("extend"); setSelected([...newConversationFrom.participantRefs]); setQuery(""); }}>Add people or agents</Button>}
    {mode !== "closed" && <form aria-label={mode === "extend" ? "Start new conversation" : "Create conversation"} className="room-creation" aria-busy={creationBusy}
      onSubmit={event => {
        event.preventDefault();
        if (!canCreate || creationBusy) return;
        if (mode === "extend") onNewConversation?.({ participantRefs: selected });
        else onCreate?.({ kind, ...(kind !== "conversation" ? { name: name.trim() } : {}), participantRefs: selected });
      }}>
      <h3 className="font-semibold">{mode === "extend" ? "Bring someone into the conversation" : "New conversation"}</h3>
      {mode === "extend" ? <p className="text-sm text-muted-foreground">This starts a new conversation with fresh history. The original messages stay in this conversation.</p> : <>
        <label htmlFor={`${id}-kind`}>Conversation type</label>
        <select id={`${id}-kind`} className="room-input" value={kind} disabled={creationBusy} onChange={event => setKind(event.target.value as CreationKind)}>
          {permittedCreation.map(value => <option key={value} value={value}>{value === "conversation" ? "Direct or group message" : roomKindLabel[value]}</option>)}
        </select>
        {kind !== "conversation" && <><label htmlFor={`${id}-name`}>Room name</label><input id={`${id}-name`} className="room-input" value={name} maxLength={80} disabled={creationBusy} onChange={event => setName(event.target.value)} /></>}
        <p className="text-sm text-muted-foreground">{kind === "public" ? "Visible to your organization. Visibility is fixed at creation." : kind === "private" ? "Only invited participants can read this room. Visibility is fixed at creation." : "Choose people and agents for a new conversation."}</p>
      </>}
      {selected.length > 0 && <p className="text-sm room-wrap">Selected: {selected.map(ref => participants.find(person => person.ref === ref)?.name ?? "Unavailable participant").join(", ")}</p>}
      <div className="room-composer-actions"><Button type="button" variant="ghost" disabled={creationBusy} onClick={() => setMode("closed")}>Cancel</Button>
        <Button type="submit" disabled={!canCreate || creationBusy}>{creationBusy ? "Creating…" : mode === "extend" || kind === "conversation" ? "Start new conversation" : "Create room"}</Button></div>
      {creationError && <p role="alert" className="text-sm text-err">{creationError}</p>}
    </form>}
    <section aria-label="People and agents">
      <Button variant="ghost" className="w-full justify-start" aria-expanded={directoryOpen || mode !== "closed"}
        aria-controls={`${id}-people`} onClick={() => setDirectoryOpen(!directoryOpen)}><Users aria-hidden="true" />People and agents</Button>
      {(directoryOpen || mode !== "closed") && <div id={`${id}-people`} className="room-picker">
        <label htmlFor={`${id}-search`} className="sr-only">Find people and agents</label>
        <input id={`${id}-search`} className="room-input" type="search" placeholder="Find a person or agent…" value={query} onChange={event => setQuery(event.target.value)} />
        <div className="room-picker-list">{visibleParticipants.map(person => <label className="room-choice" key={person.ref}>
          {mode !== "closed" && <input type="checkbox" checked={selected.includes(person.ref)} disabled={creationBusy || (mode === "extend" && newConversationFrom?.participantRefs.includes(person.ref))}
            onChange={event => setSelected(event.target.checked ? [...selected, person.ref] : selected.filter(ref => ref !== person.ref))} />}
          <Identity name={person.name} avatarOnly size="sm" />
          <span className="min-w-0"><span className="block truncate" title={person.name}>{person.name}</span><span className="text-sm text-muted-foreground">{person.kind === "agent" ? "Agent" : "Person"}</span></span>
        </label>)}</div>
        {visibleParticipants.length === 0 && <p className="text-sm text-muted-foreground">No matching people or agents.</p>}
      </div>}
    </section>
  </nav>;
}
