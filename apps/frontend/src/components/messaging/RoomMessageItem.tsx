"use client";

import type { ReactElement, ReactNode } from "react";
import { MessageCircle } from "lucide-react";
import type { Components, UrlTransform } from "react-markdown";
import { Button } from "@/components/ui/button";
import { Identity } from "@/components/teams/shared/components/Identity";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { cn } from "@/lib/utils";
import { EmojiPicker } from "./EmojiPicker";
import { ParticipantAvatar } from "./ParticipantAvatar";
import { ParticipantProfile } from "./ParticipantProfile";
import { RoomImageAttachments } from "./RoomImageAttachments";
import { RoomMessageContent } from "./RoomMessageContent";
import type { RoomMessage, RoomParticipant, WorkState } from "./types";
import type { RoomPanelProps } from "./RoomPanel";

export interface RoomMessageItemProps {
  id: string;
  message: RoomMessage;
  text: string;
  participant?: RoomParticipant;
  currentUserId: string;
  actionable: boolean;
  inThread: boolean;
  collapsed: boolean;
  startEditing?: boolean;
  onEditingClosed?: () => void;
  replyCount: number;
  highlighted: boolean;
  mentionsViewer: boolean;
  markdown: Partial<Components>;
  urlTransform: UrlTransform;
  workStatus: (state: WorkState) => ReactNode;
  quickReactions: readonly string[];
  reactionPending: boolean;
  reactionError: boolean;
  onToggleReaction?: (emoji: string) => void;
  onOpenThread?: (button: HTMLButtonElement) => void;
  onSelectParticipant?: RoomPanelProps["onSelectParticipant"];
  onUpdateMessage?: RoomPanelProps["onUpdateMessage"];
  onDownloadAttachment?: RoomPanelProps["onDownloadAttachment"];
  toolbarExtras?: ReactNode;
}

export function RoomMessageItem({ id, message, text, participant, currentUserId, actionable, inThread, collapsed, startEditing, onEditingClosed, replyCount, highlighted, mentionsViewer, markdown, urlTransform, workStatus, quickReactions, reactionPending, reactionError, onToggleReaction, onOpenThread, onSelectParticipant, onUpdateMessage, onDownloadAttachment, toolbarExtras }: RoomMessageItemProps): ReactElement {
  const name = participant?.displayName || message.author.displayName || "Unknown author";
  const timestamp = !Number.isNaN(Date.parse(message.createdAt)) ? message.createdAt : undefined;
  const time = timestamp ? new Date(timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
  const appliedReactions = Object.entries(message.reactions ?? {}).filter(([, users]) => users.length > 0);
  const actions = ((!message.deletedAt && onToggleReaction) || !inThread || toolbarExtras) ? <>
    {!message.deletedAt && onToggleReaction && <>
      <span className="room-quick-reactions" aria-label="Quick reactions">{quickReactions.map((emoji) => <Button key={emoji} type="button" variant="ghost" size="icon-sm"
        aria-label={`React ${emoji}`} title={`React ${emoji}`} disabled={!actionable || reactionPending} onClick={() => onToggleReaction(emoji)}>{emoji}</Button>)}</span>
      <EmojiPicker onPick={onToggleReaction} disabled={!actionable || reactionPending} />
    </>}
    {!inThread && onOpenThread && <Button type="button" variant="ghost" size="icon-sm" aria-label="Reply in thread" title="Reply in thread" disabled={!actionable}
      onClick={(event) => onOpenThread(event.currentTarget)}><MessageCircle aria-hidden="true" /></Button>}
    {toolbarExtras}
  </> : undefined;
  return <article id={id} tabIndex={-1} className={cn("room-message", collapsed && "room-message-collapsed", highlighted && "bg-accent", mentionsViewer && "room-message-mentions-me")} aria-label={`Message from ${name}`}>
    {collapsed
      ? <span className="room-message-gutter" aria-hidden="true">{time}</span>
      : participant ? <ParticipantAvatar participant={participant} /> : <Identity name={name} avatarOnly size="default" />}
    <div className="room-message-content">
      {collapsed && timestamp && <time className="sr-only" dateTime={timestamp}>{time}</time>}
      {!collapsed && <div className="room-message-byline">{participant
        ? <ParticipantProfile participant={participant} currentUserId={currentUserId} onMessage={onSelectParticipant} disabled={!actionable} />
        : <span className="font-semibold truncate" title={name}>{name}</span>}
        {message.author.kind === "agent" && <span className="room-agent-label">Agent</span>}
        {timestamp && <time className="text-sm text-muted-foreground" dateTime={timestamp} title={new Date(timestamp).toLocaleString()}>{time}</time>}
      </div>}
      <RoomMessageContent message={message} currentUserId={currentUserId} actionable={actionable} onUpdate={onUpdateMessage} startEditing={startEditing} onEditingClosed={onEditingClosed} actions={actions}>
        <MarkdownView skipHtml components={markdown} urlTransform={urlTransform}>{text}</MarkdownView>
      </RoomMessageContent>
      {!message.deletedAt && onToggleReaction && appliedReactions.length > 0 && <div className="flex flex-wrap gap-1 mt-1" aria-label="Message reactions">
        {appliedReactions.map(([emoji, users]) => {
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
