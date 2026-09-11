"use client";

import type { ReactElement, ReactNode } from "react";
import { MessageCircle, SmilePlus } from "lucide-react";
import type { Components } from "react-markdown";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Identity } from "@/components/teams/shared/components/Identity";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { cn } from "@/lib/utils";
import { ParticipantAvatar } from "./ParticipantAvatar";
import { ParticipantProfile } from "./ParticipantProfile";
import { RoomImageAttachments } from "./RoomImageAttachments";
import { RoomMessageContent } from "./RoomMessageContent";
import type { RoomMessage, RoomParticipant, WorkState } from "./types";
import type { RoomPanelProps } from "./RoomPanel";

const reactionEmojis = ["👍", "❤️", "😂", "🎉", "👀", "✅"] as const; // replaced by the picker in Task 6

export interface RoomMessageItemProps {
  id: string;
  message: RoomMessage;
  participant?: RoomParticipant;
  currentUserId: string;
  actionable: boolean;
  inThread: boolean;
  collapsed: boolean;
  replyCount: number;
  highlighted: boolean;
  markdown: Partial<Components>;
  workStatus: (state: WorkState) => ReactNode;
  reactionPending: boolean;
  reactionError: boolean;
  onToggleReaction?: (emoji: string) => void;
  onOpenThread?: (button: HTMLButtonElement) => void;
  onSelectParticipant?: RoomPanelProps["onSelectParticipant"];
  onUpdateMessage?: RoomPanelProps["onUpdateMessage"];
  onDownloadAttachment?: RoomPanelProps["onDownloadAttachment"];
  toolbarExtras?: ReactNode;
}

export function RoomMessageItem({ id, message, participant, currentUserId, actionable, inThread, collapsed, replyCount, highlighted, markdown, workStatus, reactionPending, reactionError, onToggleReaction, onOpenThread, onSelectParticipant, onUpdateMessage, onDownloadAttachment, toolbarExtras }: RoomMessageItemProps): ReactElement {
  const name = participant?.displayName || message.author.displayName || "Unknown author";
  const timestamp = !Number.isNaN(Date.parse(message.createdAt)) ? message.createdAt : undefined;
  const time = timestamp ? new Date(timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
  const appliedReactions = reactionEmojis.filter((emoji) => (message.reactions?.[emoji]?.length ?? 0) > 0);
  const actions = ((!message.deletedAt && onToggleReaction) || !inThread || toolbarExtras) ? <>
    {!message.deletedAt && onToggleReaction && <DropdownMenu>
      <DropdownMenuTrigger asChild><Button type="button" variant="ghost" size="icon-sm" aria-label="Add reaction" title="Add reaction" disabled={!actionable || reactionPending}><SmilePlus aria-hidden="true" /></Button></DropdownMenuTrigger>
      <DropdownMenuContent align="end" aria-label="Choose a reaction" onEscapeKeyDown={(event) => event.stopPropagation()}>
        {reactionEmojis.map((emoji) => <DropdownMenuItem key={emoji} disabled={!actionable || reactionPending}
          aria-label={`${message.reactions?.[emoji]?.includes(currentUserId) ? "Remove" : "Add"} ${emoji} reaction`}
          onSelect={() => onToggleReaction(emoji)}><span aria-hidden="true">{emoji}</span><span>{message.reactions?.[emoji]?.includes(currentUserId) ? "Remove" : "Add"} reaction</span></DropdownMenuItem>)}
      </DropdownMenuContent>
    </DropdownMenu>}
    {!inThread && onOpenThread && <Button type="button" variant="ghost" size="icon-sm" aria-label="Reply in thread" title="Reply in thread" disabled={!actionable}
      onClick={(event) => onOpenThread(event.currentTarget)}><MessageCircle aria-hidden="true" /></Button>}
    {toolbarExtras}
  </> : undefined;
  return <article id={id} tabIndex={-1} className={cn("room-message", collapsed && "room-message-collapsed", highlighted && "bg-accent")} aria-label={`Message from ${name}`}>
    {collapsed
      ? <span className="room-message-gutter" aria-hidden="true">{time}</span>
      : participant ? <ParticipantAvatar participant={participant} /> : <Identity name={name} avatarOnly size="default" />}
    <div className="room-message-content">
      {!collapsed && <div className="room-message-byline">{participant
        ? <ParticipantProfile participant={participant} currentUserId={currentUserId} onMessage={onSelectParticipant} disabled={!actionable} />
        : <span className="font-semibold truncate" title={name}>{name}</span>}
        {message.author.kind === "agent" && <span className="room-agent-label">Agent</span>}
        {timestamp && <time className="text-sm text-muted-foreground" dateTime={timestamp} title={new Date(timestamp).toLocaleString()}>{time}</time>}
      </div>}
      <RoomMessageContent message={message} currentUserId={currentUserId} actionable={actionable} onUpdate={onUpdateMessage} actions={actions}>
        <MarkdownView skipHtml components={markdown}>{message.text}</MarkdownView>
      </RoomMessageContent>
      {!message.deletedAt && onToggleReaction && appliedReactions.length > 0 && <div className="flex flex-wrap gap-1 mt-1" aria-label="Message reactions">
        {appliedReactions.map((emoji) => {
          const users = message.reactions?.[emoji] ?? [];
          const selected = users.includes(currentUserId);
          return <Button key={emoji} type="button" size="sm" variant={selected ? "secondary" : "ghost"}
            aria-label={`${selected ? "Remove" : "Add"} ${emoji} reaction${users.length ? `, ${users.length}` : ""}`} aria-pressed={selected}
            disabled={!actionable || reactionPending} onClick={() => onToggleReaction(emoji)}>{emoji}<span>{users.length}</span></Button>;
        })}
      </div>}
      {reactionError && <p role="alert" className="text-sm text-err">Could not update reaction. Try again.</p>}
      {!message.deletedAt && !!message.attachments?.length && onDownloadAttachment && <RoomImageAttachments roomId={message.roomId} attachments={message.attachments} download={onDownloadAttachment} />}
      {message.run && <div className="room-run">{workStatus(message.run.state)}
        {message.run.details && <details><summary>Run details</summary><pre>{message.run.details.slice(0, 8192)}</pre>{message.run.details.length > 8192 && <p className="text-sm text-muted-foreground">Further details omitted.</p>}</details>}
      </div>}
      {!inThread && replyCount > 0 && onOpenThread && <Button type="button" variant="ghost" className="room-reply" disabled={!actionable}
        aria-label={`Reply in thread, ${replyCount} ${replyCount === 1 ? "reply" : "replies"}`}
        onClick={(event) => onOpenThread(event.currentTarget)}>{replyCount} {replyCount === 1 ? "reply" : "replies"}</Button>}
    </div>
  </article>;
}
