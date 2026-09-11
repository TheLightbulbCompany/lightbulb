"use client";

import { useId, useRef, useState, type ReactNode } from "react";
import { Bell, BellOff, Hash, Link, LogOut, MessageCircle, MoreHorizontal, Pencil, Pin, Star, Users } from "lucide-react";
import type { MemberPrefsRequest } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { copyText, roomPermalink } from "./messageLinks";
import { ParticipantAvatar } from "./ParticipantAvatar";
import { useAsyncAction } from "./useAsyncAction";
import type { RoomParticipant, RoomSummary } from "./types";

const FACES = 4;

export interface RoomHeaderProps {
  room?: RoomSummary;
  roomName: string;
  counterparts: readonly RoomParticipant[];
  participantCount: number;
  canEditRoom: boolean; // owner/admin AND kind !== "dm"
  canLeave: boolean; // kind === "channel" AND role !== "owner"
  actionable: boolean;
  pinnedCount: number; // shown beside the facepile; the affordance opens the Details panel's Pins tab
  detailsOpen: boolean; // whether the Details panel these affordances disclose is open
  search: ReactNode; // the shipped <RoomMessageSearch />
  onOpenDetails: (tab?: "members" | "pins" | "files" | "links" | "about") => void;
  onSetPrefs?: (prefs: MemberPrefsRequest) => Promise<void>;
  onRenameRoom?: (name: string) => Promise<void>;
  onSetTopic?: (topic: string | null) => Promise<void>;
  onLeaveRoom?: () => Promise<void>;
}

export function RoomHeader({
  room,
  roomName,
  counterparts,
  participantCount,
  canEditRoom,
  canLeave,
  actionable,
  pinnedCount,
  detailsOpen,
  search,
  onOpenDetails,
  onSetPrefs,
  onRenameRoom,
  onSetTopic,
  onLeaveRoom,
}: RoomHeaderProps) {
  const id = useId();
  const [editingTopic, setEditingTopic] = useState(false);
  const [topicDraft, setTopicDraft] = useState(room?.topic ?? "");
  const [renaming, setRenaming] = useState(false);
  const [nameDraft, setNameDraft] = useState(room?.name ?? "");
  const [leaving, setLeaving] = useState(false);
  const { pending, error, run, reset } = useAsyncAction();
  const topicTrigger = useRef<HTMLButtonElement>(null);
  const optionsTrigger = useRef<HTMLButtonElement>(null);
  const renameInput = useRef<HTMLInputElement>(null);
  const leaveCancel = useRef<HTMLButtonElement>(null);
  // Adjust state during render (React's documented alternative to an effect
  // for "reset local state when a prop changes") rather than setState-in-effect.
  const [syncedTopic, setSyncedTopic] = useState(room?.topic ?? null);
  if (syncedTopic !== (room?.topic ?? null)) {
    setSyncedTopic(room?.topic ?? null);
    setTopicDraft(room?.topic ?? "");
  }

  function restoreOptionsFocus() { optionsTrigger.current?.focus(); }

  const Icon = room?.kind === "channel" ? Hash : room?.kind === "group-dm" ? Users : MessageCircle;
  const faces = counterparts.slice(0, FACES);
  const overflow = Math.max(0, participantCount - faces.length);

  return <header className="room-header">
    <div className="room-title">
      {room?.kind === "dm" && counterparts.length === 1
        ? <ParticipantAvatar participant={counterparts[0]} size="sm" />
        : <Icon size={20} aria-hidden="true" />}
      <h2 className="font-semibold text-lg truncate" title={roomName}>{roomName}</h2>
      {editingTopic ? <form className="room-topic-form" onSubmit={(event) => {
        event.preventDefault();
        void run(async () => { await onSetTopic!(topicDraft.trim() || null); }, "The topic could not be saved.", () => setEditingTopic(false));
      }}>
        <label className="sr-only" htmlFor={`${id}-topic`}>Topic</label>
        <input id={`${id}-topic`} className="room-input" autoFocus maxLength={250} value={topicDraft} disabled={pending}
          onChange={(event) => setTopicDraft(event.target.value)}
          onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); setEditingTopic(false); setTopicDraft(room?.topic ?? ""); topicTrigger.current?.focus(); } }} />
      </form> : ((onSetTopic && canEditRoom) || room?.topic) ? <>
        <span aria-hidden="true" className="room-topic-dot">·</span>
        {onSetTopic && canEditRoom
          ? <Button ref={topicTrigger} type="button" variant="ghost" className="room-topic" disabled={!actionable}
              aria-label={room?.topic ? "Edit topic" : "Add a topic"} onClick={() => { reset(); setEditingTopic(true); }}>
              {room?.topic ? <span className="truncate">{room.topic}</span> : <><Pencil size={12} aria-hidden="true" />Add a topic</>}
            </Button>
          : <span className="room-topic truncate" title={room?.topic ?? undefined}>{room?.topic}</span>}
      </> : null}
    </div>
    <div className="room-audience">
      {search}
      {pinnedCount > 0 && <Button type="button" variant="ghost" size="sm" aria-expanded={detailsOpen}
        aria-label={`${pinnedCount} pinned ${pinnedCount === 1 ? "message" : "messages"}`} onClick={() => onOpenDetails("pins")}>
        <Pin size={14} aria-hidden="true" />{pinnedCount}</Button>}
      <Button type="button" variant="ghost" className="room-facepile" aria-expanded={detailsOpen} aria-label={`View members, ${participantCount} in this conversation`}
        onClick={() => onOpenDetails("members")}>
        {faces.map((participant) => <ParticipantAvatar key={`${participant.kind}:${participant.id}`} participant={participant} size="sm" />)}
        {overflow > 0 && <span className="text-sm text-muted-foreground">+{overflow}</span>}
      </Button>
      {onSetPrefs && <Button type="button" variant="ghost" size="icon" disabled={!actionable || pending}
        aria-pressed={!!room?.starred} aria-label={room?.starred ? "Unstar this conversation" : "Star this conversation"}
        onClick={() => void run(async () => { await onSetPrefs({ starred: !room?.starred }); }, "The star could not be saved.")}>
        <Star aria-hidden="true" fill={room?.starred ? "currentColor" : "none"} /></Button>}
      {onSetPrefs && <Button type="button" variant="ghost" size="icon" disabled={!actionable || pending}
        aria-pressed={!!room?.muted} aria-label={room?.muted ? "Unmute this conversation" : "Mute this conversation"}
        onClick={() => void run(async () => { await onSetPrefs({ muted: !room?.muted }); }, "The notification setting could not be saved.")}>
        {room?.muted ? <BellOff aria-hidden="true" /> : <Bell aria-hidden="true" />}</Button>}
      <DropdownMenu>
        <DropdownMenuTrigger asChild><Button ref={optionsTrigger} type="button" variant="ghost" size="icon" aria-label="Conversation options"><MoreHorizontal aria-hidden="true" /></Button></DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onSelect={() => void copyText(roomPermalink(room?.roomId ?? ""))}><Link aria-hidden="true" />Copy link</DropdownMenuItem>
          {onRenameRoom && canEditRoom && <DropdownMenuItem onSelect={() => { reset(); setNameDraft(room?.name ?? ""); setRenaming(true); }}><Pencil aria-hidden="true" />Rename</DropdownMenuItem>}
          <DropdownMenuItem onSelect={() => onOpenDetails()}>Details</DropdownMenuItem>
          {onLeaveRoom && canLeave && <><DropdownMenuSeparator />
            <DropdownMenuItem variant="destructive" onSelect={() => { reset(); setLeaving(true); }}><LogOut aria-hidden="true" />Leave</DropdownMenuItem></>}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
    {error && !renaming && !leaving && <p role="alert" className="room-header-error text-sm text-err">{error}</p>}
    <Dialog open={renaming} onOpenChange={(open) => { if (!open && !pending) { setRenaming(false); reset(); } }}>
      <DialogContent className="max-w-[calc(100%-2rem)] sm:max-w-md max-h-[calc(100dvh-2rem)] overflow-y-auto"
        onOpenAutoFocus={(event) => { event.preventDefault(); renameInput.current?.focus(); }}
        onCloseAutoFocus={(event) => { event.preventDefault(); restoreOptionsFocus(); }}>
        <DialogHeader><DialogTitle>Rename conversation</DialogTitle>
          <DialogDescription>Choose a name that helps your team find this conversation.</DialogDescription>
        </DialogHeader>
        <form className="grid gap-4" onSubmit={(event) => {
          event.preventDefault();
          void run(async () => { await onRenameRoom!(nameDraft.trim()); }, "The name could not be saved.", () => setRenaming(false));
        }}>
          <div className="grid gap-2">
            <label htmlFor={`${id}-rename`} className="text-sm font-medium">Conversation name</label>
            <input ref={renameInput} id={`${id}-rename`} className="room-input" value={nameDraft} maxLength={200} required disabled={pending}
              onChange={(event) => setNameDraft(event.target.value)} />
          </div>
          {error && <p role="alert" className="text-sm text-err break-words">{error}</p>}
          <DialogFooter className="gap-2">
            <Button type="button" variant="outline" disabled={pending} onClick={() => { setRenaming(false); reset(); }}>Cancel</Button>
            <Button type="submit" disabled={!actionable || pending || !nameDraft.trim()}>{pending ? "Saving…" : "Save name"}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
    <Dialog open={leaving} onOpenChange={(open) => { if (!open && !pending) { setLeaving(false); reset(); } }}>
      <DialogContent className="max-w-[calc(100%-2rem)] sm:max-w-md max-h-[calc(100dvh-2rem)] overflow-y-auto"
        onOpenAutoFocus={(event) => { event.preventDefault(); leaveCancel.current?.focus(); }}
        onCloseAutoFocus={(event) => { event.preventDefault(); restoreOptionsFocus(); }}>
        <DialogHeader><DialogTitle className="break-words">Leave {roomName}?</DialogTitle>
          <DialogDescription>You will lose access to this conversation and its messages.</DialogDescription>
        </DialogHeader>
        {error && <p role="alert" className="text-sm text-err break-words">{error}</p>}
        <DialogFooter className="gap-2">
          <Button ref={leaveCancel} type="button" variant="outline" disabled={pending} onClick={() => { setLeaving(false); reset(); }}>Cancel</Button>
          <Button type="button" variant="destructive" disabled={!actionable || pending}
            onClick={() => void run(async () => { await onLeaveRoom!(); }, "The channel could not be left.", () => setLeaving(false))}>
            {pending ? "Leaving…" : "Leave channel"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </header>;
}
