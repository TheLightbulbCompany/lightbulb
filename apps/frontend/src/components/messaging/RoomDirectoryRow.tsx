"use client";

import { useRef } from "react";
import { Bell, BellOff, Folder, Link, LogOut, MailOpen, MessageCircle, MoreHorizontal, Pencil, Star, UserPlus, Users } from "lucide-react";
import type { RoomSection } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { ParticipantAvatar } from "./ParticipantAvatar";
import { roomKindLabel, type DirectoryRoom } from "./types";

interface RoomDirectoryRowProps {
  room: DirectoryRoom;
  label: string;
  active: boolean;
  busy: boolean;
  /** The room has an unsent draft — PR B's pencil glyph. */
  draft?: boolean;
  onSelect: () => void;
  /** Star/mute are omitted from the menu when the caller cannot persist them. */
  onToggleStar?: () => void;
  onToggleMute?: () => void;
  onMarkUnread?: () => void;
  onCopyLink: () => void;
  onAddPeople?: () => void;
  onMoveToSection?: (sectionId: string | null) => void;
  sections: readonly RoomSection[];
  /** Tasks in this room waiting on the viewer: a warn dot (never counted twice: the number lives on Activity). */
  needsYou?: number;
  /** Super-agent: unread is the row's weight, never a number, so needsYouCount is the only number on screen (07 §1). */
  noCounts?: boolean;
  /** Handed this row's overflow trigger so the confirm dialog can return focus to it. */
  onLeave?: (trigger: HTMLButtonElement | null) => void;
}

const SHARED_ROOM_HINT = "Your direct line to the Lightbulb team";

export function RoomDirectoryRow({ room, label, active, busy, draft, onSelect, onToggleStar, onToggleMute, onMarkUnread, onCopyLink, onAddPeople, onMoveToSection, sections, onLeave, needsYou = 0, noCounts = false }: RoomDirectoryRowProps) {
  const actionsTrigger = useRef<HTMLButtonElement>(null);
  const Icon = room.kind === "channel" ? Folder : room.kind === "group-dm" ? Users : MessageCircle;
  // A muted room never carries a badge (spec: muted rows are muted-coloured,
  // with the word "Muted" so colour is never the only signal).
  const badge = !room.muted && room.unreadCount > 0;
  const faces = room.participants.slice(0, 2);
  const agentDm = room.kind === "dm" && room.participants.length === 1 && room.participants[0].kind === "agent";
  // Its own name, said once (dev walk W8: the text sat in child spans and read as an unnamed button): the room, what it
  // is, then the marks the row shows.
  const name = [label, roomKindLabel[room.kind], agentDm && "Agent", room.starred && "Starred", room.muted && "Muted", room.shared && "Shared",
    needsYou > 0 && `${needsYou} need${needsYou === 1 ? "s" : ""} you`, badge && (noCounts ? "Unread" : `${room.unreadCount} unread messages`), draft && "Unsent draft"].filter(Boolean).join(", ");
  return <li className="room-directory-row">
    <button type="button" className={`room-directory-entry ${badge ? "font-semibold" : ""} ${room.muted ? "room-directory-muted" : ""}`}
      aria-current={active ? "page" : undefined} aria-label={name} onClick={onSelect}>
      {room.kind === "dm" && room.participants.length === 1
        ? <ParticipantAvatar participant={room.participants[0]} size="xs" />
        : room.kind === "group-dm" && faces.length > 0
          ? <span className="room-directory-faces" aria-hidden="true">{faces.map((participant) => <ParticipantAvatar key={`${participant.kind}:${participant.id}`} participant={participant} size="xs" showPresence={false} />)}</span>
          : <Icon size={16} aria-hidden="true" />}
      <span className="min-w-0 flex-1">
        <span className="block truncate" title={label}>{label}</span>
        {room.shared && <span className="block truncate text-caption text-muted-foreground">{SHARED_ROOM_HINT}</span>}
        <span className="sr-only">{roomKindLabel[room.kind]}</span>
      </span>
      {/* Same tag the roster rows carry, so an agent reads as an agent whether or not a DM exists yet. */}
      {agentDm && <span className="room-directory-tag">Agent</span>}
      {room.starred && <Star size={12} role="img" aria-label="Starred" className="room-directory-star" />}
      {room.muted && <span className="room-directory-tag">Muted</span>}
      {room.shared && <span className="room-directory-tag" title={SHARED_ROOM_HINT}>Shared</span>}
      {needsYou > 0 && <span aria-hidden="true" data-needs-you className="size-2 shrink-0 rounded-full bg-warn" />}
      {badge && !noCounts && <span className="room-count" aria-label={`${room.unreadCount} unread messages`}>{room.unreadCount}</span>}
      {draft && <Pencil size={14} role="img" aria-label="Unsent draft" className="text-muted-foreground" />}
    </button>
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button ref={actionsTrigger} variant="ghost" size="icon" className="room-directory-actions" aria-label={`Actions for ${label}`} aria-busy={busy} data-room-id={room.roomId}><MoreHorizontal aria-hidden="true" /></Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {/* The trigger itself stays enabled (see aria-busy above) so Radix has
            somewhere to return focus to -- these items are what actually gate
            re-entry while this room's own save is in flight. */}
        {onToggleStar && <DropdownMenuItem disabled={busy} onSelect={onToggleStar}><Star aria-hidden="true" />{room.starred ? "Unstar" : "Star"}</DropdownMenuItem>}
        {onToggleMute && <DropdownMenuItem disabled={busy} onSelect={onToggleMute}>{room.muted ? <Bell aria-hidden="true" /> : <BellOff aria-hidden="true" />}{room.muted ? "Unmute" : "Mute"}</DropdownMenuItem>}
        {onMarkUnread && <DropdownMenuItem disabled={busy} onSelect={onMarkUnread}><MailOpen aria-hidden="true" />Mark unread</DropdownMenuItem>}
        <DropdownMenuItem onSelect={onCopyLink}><Link aria-hidden="true" />Copy link</DropdownMenuItem>
        {onAddPeople && <DropdownMenuItem onSelect={onAddPeople}><UserPlus aria-hidden="true" />Add people</DropdownMenuItem>}
        {onMoveToSection && <>
          <DropdownMenuSeparator />
          {sections.map((section) => <DropdownMenuItem key={section.id} disabled={busy || room.sectionId === section.id} onSelect={() => onMoveToSection(section.id)}>Move to {section.name}</DropdownMenuItem>)}
          {room.sectionId && <DropdownMenuItem disabled={busy} onSelect={() => onMoveToSection(null)}>Remove from section</DropdownMenuItem>}
        </>}
        {onLeave && <>
          <DropdownMenuSeparator />
          <DropdownMenuItem variant="destructive" onSelect={() => onLeave(actionsTrigger.current)}><LogOut aria-hidden="true" />{room.kind === "group-dm" ? "Leave group chat" : "Leave project"}</DropdownMenuItem>
        </>}
      </DropdownMenuContent>
    </DropdownMenu>
  </li>;
}
