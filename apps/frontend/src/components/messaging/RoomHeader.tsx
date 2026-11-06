"use client";

import { useId, useRef, useState, type ReactNode } from "react";
import { Archive, ArchiveRestore, Bell, BellOff, Folder, Link, LogOut, MessageCircle, Mic, MoreHorizontal, Pencil, Pin, Star, Trash2, UserPlus, Users } from "lucide-react";
import { liveMarker, type MemberPrefsRequest, type RoomSummary } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { copyText, roomPermalink } from "./messageLinks";
import { ParticipantAvatar } from "./ParticipantAvatar";
import { useRoomHuddle } from "@/components/huddle/useRoomHuddle";
import { useHuddle } from "@/components/huddle/HuddleProvider";
import { LiveStrip } from "@/components/huddle/LiveStrip";
import { useAsyncAction } from "./useAsyncAction";
import type { RoomParticipant } from "./types";

const FACES = 4;

interface RoomHeaderProps {
  room?: RoomSummary;
  roomName: string;
  counterparts: readonly RoomParticipant[];
  participants?: readonly RoomParticipant[]; // everyone, the viewer included; names the on-a-call marker
  participantCount: number;
  canEditRoom: boolean; // owner/admin AND kind !== "dm"
  canLeave: boolean; // a channel or group chat, unless the viewer owns a channel alone
  // kind === "channel", the viewer owns it and nobody else is in it (not shared): the
  // backend refuses that leave, so the Leave dialog explains it instead of offering an
  // action that will 400, and offers Delete channel in its place when possible.
  ownerCannotLeave?: boolean;
  ownerHandsOff?: boolean; // the viewer owns this channel and leaving hands it to someone else
  actionable: boolean;
  pinnedCount: number; // shown beside the facepile; the affordance opens the Details panel's Pins tab
  search: ReactNode; // the shipped <RoomMessageSearch />
  views?: ReactNode; // RoomPanel's About button
  menuItems?: ReactNode; // RoomPanel's room tools (Ideas, Browser, Agent files), in the ··· menu
  onOpenDetails: (tab?: "threads" | "members" | "pins" | "files" | "links" | "about") => void;
  onAddPeople?: () => void;
  /** Direct header action for channel owners/admins; overflow behavior remains broader. */
  onQuickAddPeople?: () => void;
  onSetPrefs?: (prefs: MemberPrefsRequest) => Promise<void>;
  onRenameRoom?: (name: string) => Promise<void>;
  onSetTopic?: (topic: string | null) => Promise<void>;
  onLeaveRoom?: () => Promise<void>;
  onSetArchived?: (archived: boolean) => Promise<void>;
  onDeleteRoom?: () => Promise<void>; // passed only to the channel's owner
}

function liveNames(userIds: readonly string[], participants: readonly RoomParticipant[]) {
  return userIds.map((id) => participants.find((p) => p.kind === "human" && p.id === id)?.displayName ?? "Someone");
}

export function RoomHeader({
  room,
  roomName,
  counterparts,
  participants = counterparts,
  participantCount,
  canEditRoom,
  canLeave,
  ownerCannotLeave,
  ownerHandsOff,
  actionable,
  pinnedCount,
  search,
  views,
  menuItems,
  onOpenDetails,
  onAddPeople,
  onQuickAddPeople,
  onSetPrefs,
  onRenameRoom,
  onSetTopic,
  onLeaveRoom,
  onSetArchived,
  onDeleteRoom,
}: RoomHeaderProps) {
  const id = useId();
  const huddle = useHuddle();
  const { userIds: huddlers, available: huddlesOn } = useRoomHuddle(room && !room.guest ? room.roomId : undefined);
  const [editingTopic, setEditingTopic] = useState(false);
  const [topicDraft, setTopicDraft] = useState(room?.topic ?? "");
  const [renaming, setRenaming] = useState(false);
  const [nameDraft, setNameDraft] = useState(room?.name ?? "");
  const [leaving, setLeaving] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteDraft, setDeleteDraft] = useState("");
  // The backend refuses #general, the shared channel and DMs (ROOM_NOT_DELETABLE); don't offer them.
  const canDelete = !!onDeleteRoom && room?.kind === "channel" && !room.shared && !room.isDefault;
  // The backend refuses #general, the shared channel and 1:1 DMs (ROOM_NOT_ARCHIVABLE); don't
  // offer them. A group chat has no admins, so any member may archive it.
  const canArchive = !!onSetArchived && (room?.kind === "group-dm"
    || (canEditRoom && room?.kind === "channel" && !room.shared && !room.isDefault));
  const noun = room?.kind === "group-dm" ? "group chat" : "project";
  // What the room is called in copy: a direct message is a chat, never a project.
  const what = room?.kind === "dm" ? "chat" : noun;
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

  function cancelTopicEdit() {
    reset();
    setEditingTopic(false);
    setTopicDraft(room?.topic ?? "");
    topicTrigger.current?.focus();
  }

  const Icon = room?.kind === "channel" ? Folder : room?.kind === "group-dm" ? Users : MessageCircle;
  const faces = counterparts.slice(0, FACES);
  const overflow = Math.max(0, participantCount - faces.length);

  return <header className="room-header">
    <div className="room-title">
      {room?.kind === "dm" && counterparts.length === 1
        ? <ParticipantAvatar participant={counterparts[0]} size="sm" />
        : <Icon size={20} aria-hidden="true" />}
      <h2 className="font-semibold text-lg truncate" title={roomName}>{roomName}</h2>
      {huddlers.length > 0 && !(huddle.room && huddle.roomId === room?.roomId) && <span className="room-topic room-live" role="status"><span aria-hidden="true" className="room-live-dot" />{liveMarker(liveNames(huddlers, participants))}</span>}
      {room?.shared && <span className="room-directory-tag" title="Shared between this workspace and Lightbulb">Shared</span>}
      {editingTopic ? <form className="room-topic-form" onSubmit={(event) => {
        event.preventDefault();
        void run(async () => { await onSetTopic!(topicDraft.trim() || null); }, "The topic could not be saved.", () => setEditingTopic(false));
      }}>
        <label className="sr-only" htmlFor={`${id}-topic`}>Topic</label>
        <input id={`${id}-topic`} className="room-input" autoFocus maxLength={250} value={topicDraft} disabled={pending}
          onChange={(event) => setTopicDraft(event.target.value)}
          onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); cancelTopicEdit(); } }} />
        <Button type="submit" size="sm" disabled={pending}>{pending ? "Saving…" : "Save"}</Button>
        <Button type="button" variant="ghost" size="sm" disabled={pending} onClick={cancelTopicEdit}>Cancel</Button>
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
      {/* While Live, the strip stands in for Go live: in full here, as "Live in #x — Return" in any other room. */}
      <LiveStrip roomId={room?.roomId} members={participants} />
      {/* Every room kind; a room with no listening agent is a people-only voice room (no Lightbulb). */}
      {!huddle.room && room && huddlesOn && !room.guest && (
        <Button type="button" variant="ghost" size="sm" disabled={Boolean(huddle.joining)}
          onClick={() => void huddle.join(room.roomId, roomName)}>
          <Mic className="h-4 w-4" aria-hidden="true" /> {huddle.joining === room.roomId ? "Joining…" : "Go live"}
        </Button>
      )}
      {onQuickAddPeople && <Button type="button" variant="outline" size="sm" disabled={!actionable} onClick={onQuickAddPeople}>
        <UserPlus size={14} aria-hidden="true" />Add people
      </Button>}
      {pinnedCount > 0 && <Button type="button" variant="ghost" size="sm"
        aria-label={`${pinnedCount} pinned ${pinnedCount === 1 ? "message" : "messages"}`} onClick={() => onOpenDetails("pins")}>
        <Pin size={14} aria-hidden="true" />{pinnedCount}</Button>}
      <Button type="button" variant="ghost" className="room-facepile" aria-label={`People, ${participantCount} here`} title="People"
        onClick={() => onOpenDetails("members")}>
        {faces.map((participant) => <ParticipantAvatar key={`${participant.kind}:${participant.id}`} participant={participant} size="sm" showPresence={false} />)}
        {overflow > 0 && <span className="room-facepile-more text-ui text-muted-foreground">+{overflow}</span>}
      </Button>
      {views}
      <DropdownMenu>
        <DropdownMenuTrigger asChild><Button ref={optionsTrigger} type="button" variant="ghost" size="icon" aria-label="Options" title="More"><MoreHorizontal aria-hidden="true" /></Button></DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          {onSetPrefs && <DropdownMenuItem disabled={!actionable || pending}
            onSelect={() => void run(async () => { await onSetPrefs({ starred: !room?.starred }); }, "The star could not be saved.")}>
            <Star aria-hidden="true" fill={room?.starred ? "currentColor" : "none"} />{room?.starred ? "Unstar" : "Star"}</DropdownMenuItem>}
          {onSetPrefs && <DropdownMenuItem disabled={!actionable || pending}
            onSelect={() => void run(async () => { await onSetPrefs({ muted: !room?.muted }); }, "The notification setting could not be saved.")}>
            {room?.muted ? <Bell aria-hidden="true" /> : <BellOff aria-hidden="true" />}{room?.muted ? "Unmute" : "Mute"}</DropdownMenuItem>}
          <DropdownMenuItem onSelect={() => void copyText(roomPermalink(room?.roomId ?? ""))}><Link aria-hidden="true" />Copy link</DropdownMenuItem>
          {onRenameRoom && canEditRoom && !room?.shared && <DropdownMenuItem onSelect={() => { reset(); setNameDraft(room?.name ?? ""); setRenaming(true); }}><Pencil aria-hidden="true" />Rename</DropdownMenuItem>}
          {onAddPeople && <DropdownMenuItem onSelect={onAddPeople}><Users aria-hidden="true" />Add people</DropdownMenuItem>}
          {menuItems}
          <DropdownMenuItem onSelect={() => onOpenDetails("about")}>Details</DropdownMenuItem>
          {canArchive && (room?.archivedAt
            ? <DropdownMenuItem onSelect={() => void run(async () => { await onSetArchived!(false); }, `The ${noun} could not be unarchived.`)}><ArchiveRestore aria-hidden="true" />Unarchive {noun}</DropdownMenuItem>
            : <DropdownMenuItem onSelect={() => { reset(); setArchiving(true); }}><Archive aria-hidden="true" />Archive {noun}</DropdownMenuItem>)}
          {((onLeaveRoom && canLeave) || ownerCannotLeave) && <><DropdownMenuSeparator />
            <DropdownMenuItem variant="destructive" onSelect={() => { reset(); setLeaving(true); }}><LogOut aria-hidden="true" />Leave</DropdownMenuItem></>}
          {canDelete && <><DropdownMenuSeparator />
            <DropdownMenuItem variant="destructive" onSelect={() => { reset(); setDeleteDraft(""); setDeleting(true); }}><Trash2 aria-hidden="true" />Delete project</DropdownMenuItem></>}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
    {error && !renaming && !leaving && !archiving && !deleting && <p role="alert" className="room-header-error text-ui text-err">{error}</p>}
    <Dialog open={renaming} onOpenChange={(open) => { if (!open && !pending) { setRenaming(false); reset(); } }}>
      <DialogContent className="max-w-[calc(100%-2rem)] sm:max-w-md max-h-[calc(100dvh-2rem)] overflow-y-auto"
        onOpenAutoFocus={(event) => { event.preventDefault(); renameInput.current?.focus(); }}
        onCloseAutoFocus={(event) => { event.preventDefault(); restoreOptionsFocus(); }}>
        <DialogHeader><DialogTitle>Rename {what}</DialogTitle>
          <DialogDescription>Choose a name that helps your team find this {what}.</DialogDescription>
        </DialogHeader>
        <form className="grid gap-4" onSubmit={(event) => {
          event.preventDefault();
          void run(async () => { await onRenameRoom!(nameDraft.trim()); }, "The name could not be saved.", () => setRenaming(false));
        }}>
          <div className="grid gap-2">
            <label htmlFor={`${id}-rename`} className="text-ui font-medium">Name</label>
            <input ref={renameInput} id={`${id}-rename`} className="room-input" value={nameDraft} maxLength={200} required disabled={pending}
              onChange={(event) => setNameDraft(event.target.value)} />
          </div>
          {error && <p role="alert" className="text-ui text-err break-words">{error}</p>}
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
        <DialogHeader><DialogTitle className="break-words">{ownerCannotLeave ? `Can't leave ${roomName}` : `Leave ${roomName}?`}</DialogTitle>
          <DialogDescription>
            {ownerCannotLeave ? "You are the only person in this project. Delete it instead."
              : ownerHandsOff
                ? `You will lose access to this ${what} and its messages. Ownership passes to another member.`
                : `You will lose access to this ${what} and its messages.`}
          </DialogDescription>
        </DialogHeader>
        {error && <p role="alert" className="text-ui text-err break-words">{error}</p>}
        <DialogFooter className="gap-2">
          <Button ref={leaveCancel} type="button" variant="outline" disabled={pending} onClick={() => { setLeaving(false); reset(); }}>Cancel</Button>
          {ownerCannotLeave
            ? (canDelete && <Button type="button" variant="destructive" disabled={pending}
                onClick={() => { setLeaving(false); reset(); setDeleteDraft(""); setDeleting(true); }}>Delete project</Button>)
            : <Button type="button" variant="destructive" disabled={!actionable || pending}
                onClick={() => void run(async () => { await onLeaveRoom!(); }, `The ${noun} could not be left.`, () => setLeaving(false))}>
                {pending ? "Leaving…" : `Leave ${noun}`}
              </Button>}
        </DialogFooter>
      </DialogContent>
    </Dialog>
    <Dialog open={archiving} onOpenChange={(open) => { if (!open && !pending) { setArchiving(false); reset(); } }}>
      <DialogContent className="max-w-[calc(100%-2rem)] sm:max-w-md max-h-[calc(100dvh-2rem)] overflow-y-auto"
        onCloseAutoFocus={(event) => { event.preventDefault(); restoreOptionsFocus(); }}>
        <DialogHeader><DialogTitle className="break-words">Archive {roomName}?</DialogTitle>
          <DialogDescription>Members keep its history, but nobody can post and agents stop responding until it is unarchived.</DialogDescription>
        </DialogHeader>
        {error && <p role="alert" className="text-ui text-err break-words">{error}</p>}
        <DialogFooter className="gap-2">
          <Button type="button" variant="outline" disabled={pending} onClick={() => { setArchiving(false); reset(); }}>Cancel</Button>
          <Button type="button" disabled={!actionable || pending}
            onClick={() => void run(async () => { await onSetArchived!(true); }, `The ${noun} could not be archived.`, () => setArchiving(false))}>
            {pending ? "Archiving…" : `Archive ${noun}`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
    <Dialog open={deleting} onOpenChange={(open) => { if (!open && !pending) { setDeleting(false); reset(); } }}>
      <DialogContent className="max-w-[calc(100%-2rem)] sm:max-w-md max-h-[calc(100dvh-2rem)] overflow-y-auto"
        onCloseAutoFocus={(event) => { event.preventDefault(); restoreOptionsFocus(); }}>
        <DialogHeader><DialogTitle className="break-words">Delete {roomName}?</DialogTitle>
          <DialogDescription>The project disappears for every member and agents stop responding in it. This can&apos;t be undone.</DialogDescription>
        </DialogHeader>
        <form className="grid gap-4" onSubmit={(event) => {
          event.preventDefault();
          if (deleteDraft !== roomName) return;
          void run(async () => { await onDeleteRoom!(); }, "The project could not be deleted.", () => setDeleting(false));
        }}>
          <div className="grid gap-2">
            <label htmlFor={`${id}-delete`} className="text-ui font-medium">Type <strong className="break-words">{roomName}</strong> to confirm</label>
            <input id={`${id}-delete`} className="room-input" autoComplete="off" value={deleteDraft} disabled={pending}
              onChange={(event) => setDeleteDraft(event.target.value)} />
          </div>
          {error && <p role="alert" className="text-ui text-err break-words">{error}</p>}
          <DialogFooter className="gap-2">
            <Button type="button" variant="outline" disabled={pending} onClick={() => { setDeleting(false); reset(); }}>Cancel</Button>
            <Button type="submit" variant="destructive" disabled={!actionable || pending || deleteDraft !== roomName}>
              {pending ? "Deleting…" : "Delete project"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  </header>;
}
