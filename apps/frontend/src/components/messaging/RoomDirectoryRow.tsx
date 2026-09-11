"use client";

import { Bell, BellOff, Hash, Link, LogOut, MailOpen, MessageCircle, MoreHorizontal, Pencil, Star, Users } from "lucide-react";
import type { RoomSection } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { ParticipantAvatar } from "./ParticipantAvatar";
import { roomKindLabel, type DirectoryRoom } from "./types";

export interface RoomDirectoryRowProps {
  room: DirectoryRoom;
  label: string;
  active: boolean;
  busy: boolean;
  /** The room has an unsent draft — PR B's pencil glyph. */
  draft?: boolean;
  onSelect: () => void;
  onToggleStar: () => void;
  onToggleMute: () => void;
  onMarkUnread: () => void;
  onCopyLink: () => void;
  onMoveToSection?: (sectionId: string | null) => void;
  sections: readonly RoomSection[];
  onLeave?: () => void;
}

export function RoomDirectoryRow({ room, label, active, busy, draft, onSelect, onToggleStar, onToggleMute, onMarkUnread, onCopyLink, onMoveToSection, sections, onLeave }: RoomDirectoryRowProps) {
  const Icon = room.kind === "channel" ? Hash : room.kind === "group-dm" ? Users : MessageCircle;
  // A muted room never carries a badge (spec: muted rows are muted-coloured,
  // with the word "Muted" so colour is never the only signal).
  const badge = !room.muted && room.unreadCount > 0;
  const faces = room.participants.slice(0, 2);
  return <li className="room-directory-row">
    <button type="button" className={`room-directory-entry ${badge ? "font-semibold" : ""} ${room.muted ? "room-directory-muted" : ""}`}
      aria-current={active ? "page" : undefined} onClick={onSelect}>
      {room.kind === "dm" && room.participants.length === 1
        ? <ParticipantAvatar participant={room.participants[0]} size="sm" />
        : room.kind === "group-dm" && faces.length > 0
          ? <span className="room-directory-faces">{faces.map((participant) => <ParticipantAvatar key={`${participant.kind}:${participant.id}`} participant={participant} size="sm" />)}</span>
          : <Icon size={16} aria-hidden="true" />}
      <span className="min-w-0 flex-1">
        <span className="block truncate" title={label}>{label}</span>
        <span className="sr-only">{roomKindLabel[room.kind]}</span>
      </span>
      {room.starred && <Star size={12} aria-label="Starred" className="room-directory-star" />}
      {room.muted && <span className="room-directory-tag">Muted</span>}
      {badge && <span className="room-count" aria-label={`${room.unreadCount} unread messages`}>{room.unreadCount}</span>}
      {draft && <Pencil size={14} role="img" aria-label="Unsent draft" className="text-muted-foreground" />}
    </button>
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon" className="room-directory-actions" aria-label={`Conversation actions for ${label}`} disabled={busy}><MoreHorizontal aria-hidden="true" /></Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem onSelect={onToggleStar}><Star aria-hidden="true" />{room.starred ? "Unstar conversation" : "Star conversation"}</DropdownMenuItem>
        <DropdownMenuItem onSelect={onToggleMute}>{room.muted ? <Bell aria-hidden="true" /> : <BellOff aria-hidden="true" />}{room.muted ? "Unmute conversation" : "Mute conversation"}</DropdownMenuItem>
        <DropdownMenuItem onSelect={onMarkUnread}><MailOpen aria-hidden="true" />Mark unread</DropdownMenuItem>
        <DropdownMenuItem onSelect={onCopyLink}><Link aria-hidden="true" />Copy link</DropdownMenuItem>
        {onMoveToSection && <>
          <DropdownMenuSeparator />
          {sections.map((section) => <DropdownMenuItem key={section.id} disabled={room.sectionId === section.id} onSelect={() => onMoveToSection(section.id)}>Move to {section.name}</DropdownMenuItem>)}
          {room.sectionId && <DropdownMenuItem onSelect={() => onMoveToSection(null)}>Remove from section</DropdownMenuItem>}
        </>}
        {onLeave && <>
          <DropdownMenuSeparator />
          <DropdownMenuItem variant="destructive" onSelect={onLeave}><LogOut aria-hidden="true" />Leave channel</DropdownMenuItem>
        </>}
      </DropdownMenuContent>
    </DropdownMenu>
  </li>;
}
