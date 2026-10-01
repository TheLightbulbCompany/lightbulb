"use client";

import { useEffect, useState, type ReactElement, type ReactNode } from "react";
import { MessageCircle } from "lucide-react";
import type { Components, UrlTransform } from "react-markdown";
import { useSessionSendReceipt, type CanonicalRoomMessage, type LocalSessionCommandReceipt, type SessionSend } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Identity } from "@/components/teams/shared/components/Identity";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { cn } from "@/lib/utils";
import { EmojiPicker } from "./EmojiPicker";
import { MessageTaskCard } from "./MessageTaskCard";
import { ParticipantAvatar } from "./ParticipantAvatar";
import { ParticipantProfile } from "./ParticipantProfile";
import { RoomDocumentCards } from "./RoomDocumentCards";
import { RoomImageAttachments } from "./RoomImageAttachments";
import { RoomAppLinks } from "./RoomAppLinks";
import { RoomLinkPreviews } from "./RoomLinkPreviews";
import { RoomMessageContent } from "./RoomMessageContent";
import { ReceiptChip } from "./NativeSessionTranscript";
import type { DirectoryParticipant, RoomMessage, RoomParticipant } from "./types";
import type { RoomPanelProps } from "./RoomPanel";

interface RoomMessageItemProps {
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
  /** Changes when the containing thread gains a canonical reply, even for a row inside that thread. */
  receiptRefreshKey?: number;
  /** The newest reply's `createdAt`, for "N replies · last reply {relative}". */
  lastReplyAt?: string;
  threadParticipants?: readonly DirectoryParticipant[];
  workingNames?: readonly string[];
  /** A quiet delivery note under a follow-up: "Friday saw this" / "…will read this after the current reply". */
  receipt?: string;
  highlighted: boolean;
  mentionsViewer: boolean;
  markdown: Partial<Components>;
  urlTransform: UrlTransform;
  quickReactions: readonly string[];
  reactionPending: boolean;
  reactionError: boolean;
  onToggleReaction?: (emoji: string) => void;
  onOpenThread?: (button: HTMLButtonElement) => void;
  onSelectParticipant?: RoomPanelProps["onSelectParticipant"];
  onUpdateMessage?: RoomPanelProps["onUpdateMessage"];
  onDownloadAttachment?: RoomPanelProps["onDownloadAttachment"];
  onAttachmentUrl?: RoomPanelProps["onAttachmentUrl"];
  onGetSessionCommand?: (sessionId: string, requestId: string) => Promise<LocalSessionCommandReceipt>;
  onRetrySessionSend?: (roomId: string, messageId: string, sessionId: string, request: { requestId: string; expectedRequestId: string | null }) => Promise<CanonicalRoomMessage>;
  onCancelSessionSend?: (roomId: string, messageId: string, sessionId: string, request: { expectedRequestId: string | null }) => Promise<CanonicalRoomMessage>;
  accessibleSessionIds?: readonly string[];
  onOpenSession?: (sessionId: string) => void;
  toolbarExtras?: ReactNode;
  menuExtras?: ReactNode;
}

function SessionSendChip({ send, roomId, messageId, refreshKey, onGetSessionCommand, onRetrySessionSend, onCancelSessionSend, replied }: {
  send: SessionSend; roomId: string; messageId: string; refreshKey: number;
  onGetSessionCommand?: (sessionId: string, requestId: string) => Promise<LocalSessionCommandReceipt>;
  onRetrySessionSend?: RoomMessageItemProps["onRetrySessionSend"];
  onCancelSessionSend?: RoomMessageItemProps["onCancelSessionSend"];
  replied: boolean;
}) {
  const [current, setCurrent] = useState(send);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { setCurrent(send); setError(""); }, [send.sessionId, send.requestId, send.error]);
  const { chip, receipt } = useSessionSendReceipt(onGetSessionCommand, current, refreshKey);
  const retryable = current.error && current.error !== "CANCELLED" || receipt?.state === "rejected";
  const cancellable = retryable || receipt?.state === "pending";
  const update = async (action: "retry" | "cancel") => {
    if (busy) return;
    setBusy(true); setError("");
    try {
      const message = action === "retry"
        ? await onRetrySessionSend!(roomId, messageId, current.sessionId, { requestId: crypto.randomUUID(), expectedRequestId: current.requestId ?? null })
        : await onCancelSessionSend!(roomId, messageId, current.sessionId, { expectedRequestId: current.requestId ?? null });
      const updated = message.sessionSends?.find(item => item.sessionId === current.sessionId);
      if (updated) setCurrent(updated);
    } catch { setError("This delivery changed. Refresh the conversation and try again."); }
    finally { setBusy(false); }
  };
  if ((!chip || (replied && chip.tone !== "error")) && !error) return null;
  return <span className="flex flex-wrap items-center gap-2">
    {chip?.tone === "pending" ? <span data-session-pending role="status" className="sr-only">Waiting for the session to accept this message</span>
      : chip && <><ReceiptChip chip={chip} />{chip.tone === "error" && chip.detail && <span className="text-xs text-err">{chip.detail}</span>}</>}
    {retryable && onRetrySessionSend && <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={() => void update("retry")}>Retry</Button>}
    {cancellable && onCancelSessionSend && <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={() => void update("cancel")}>Cancel</Button>}
    {error && <span role="alert" className="text-xs text-err">{error}</span>}
  </span>;
}

export function RoomMessageItem({ id, message, text, participant, currentUserId, actionable, inThread, collapsed, startEditing, onEditingClosed, replyCount, receiptRefreshKey = replyCount, lastReplyAt, threadParticipants = [], workingNames = [], receipt, highlighted, mentionsViewer, markdown, urlTransform, quickReactions, reactionPending, reactionError, onToggleReaction, onOpenThread, onSelectParticipant, onUpdateMessage, onDownloadAttachment, onAttachmentUrl, onGetSessionCommand, onRetrySessionSend, onCancelSessionSend, accessibleSessionIds = [], onOpenSession, toolbarExtras, menuExtras }: RoomMessageItemProps): ReactElement {
  const name = participant?.displayName || message.author.displayName || "Unknown author";
  const timestamp = !Number.isNaN(Date.parse(message.createdAt)) ? message.createdAt : undefined;
  const time = timestamp ? new Date(timestamp).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "";
  const working = workingNames.length ? `${workingNames.join(", ")} ${workingNames.length === 1 ? "is" : "are"} working…` : "";
  const replyLabel = replyCount > 0 ? `${replyCount} ${replyCount === 1 ? "reply" : "replies"}` : "";
  const threadLabel = [replyLabel, working || (lastReplyAt ? `last reply ${timeAgo(lastReplyAt)}` : "")].filter(Boolean).join(" · ");
  const appliedReactions = Object.entries(message.reactions ?? {}).filter(([, users]) => users.length > 0);
  const visibleSessionSends = message.author.kind === "human" && message.author.id === currentUserId
    ? message.sessionSends : message.sessionSends?.filter((send) => !!send.error);
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
  return <article id={id} tabIndex={-1} className={cn("room-message", collapsed && "room-message-collapsed", highlighted && "room-message-highlight", mentionsViewer && "room-message-mentions-me")} aria-label={`Message from ${name}`}>
    {collapsed
      ? <span className="room-message-gutter" aria-hidden="true">{time}</span>
      : participant ? <ParticipantAvatar participant={participant} showPresence={false} /> : <Identity name={name} avatarOnly size="default" />}
    <div className="room-message-content">
      {collapsed && timestamp && <time className="sr-only" dateTime={timestamp}>{time}</time>}
      {!collapsed && <div className="room-message-byline">{participant
        ? <ParticipantProfile participant={participant} currentUserId={currentUserId} onMessage={onSelectParticipant} disabled={!actionable} />
        : <span className="font-semibold truncate" title={name}>{name}</span>}
        {message.author.kind === "agent" && <span className="room-agent-label">Agent</span>}
        {timestamp && <time className="text-sm text-muted-foreground" dateTime={timestamp} title={new Date(timestamp).toLocaleString()}>{time}</time>}
      </div>}
      <RoomMessageContent message={message} currentUserId={currentUserId} actionable={actionable} onUpdate={onUpdateMessage} startEditing={startEditing} onEditingClosed={onEditingClosed} actions={actions} menuExtras={menuExtras}>
        <MarkdownView skipHtml components={markdown} urlTransform={urlTransform}>{text}</MarkdownView>
      </RoomMessageContent>
      {message.via === "lightbulb_mcp" && !message.deletedAt && message.sessionSends?.map(send => send.displayLabel && <p key={send.sessionId} className="text-xs text-muted-foreground">
        To {onOpenSession && accessibleSessionIds.includes(send.sessionId)
          ? <button type="button" className="text-info underline underline-offset-2" aria-label={`Open session ${send.displayLabel}`} onClick={() => onOpenSession(send.sessionId)}>{send.displayLabel}</button>
          : <span>{send.displayLabel}</span>}
      </p>)}
      {!message.deletedAt && <MessageTaskCard text={message.text} prose={message.author.kind === "agent"} />}
      {!message.deletedAt && <RoomDocumentCards text={message.text} />}
      {!message.deletedAt && <RoomAppLinks text={message.text} />}
      {!message.deletedAt && <RoomLinkPreviews text={message.text} />}
      {!message.deletedAt && onToggleReaction && appliedReactions.length > 0 && <div className="flex flex-wrap gap-1 mt-1" aria-label="Message reactions">
        {appliedReactions.map(([emoji, users]) => {
          const selected = users.includes(currentUserId);
          return <Button key={emoji} type="button" size="sm" variant={selected ? "secondary" : "ghost"}
            aria-label={`${selected ? "Remove" : "Add"} ${emoji} reaction${users.length ? `, ${users.length}` : ""}`} aria-pressed={selected}
            disabled={!actionable || reactionPending} onClick={() => onToggleReaction(emoji)}>{emoji}<span>{users.length}</span></Button>;
        })}
      </div>}
      {receipt && <p role="status" className="text-xs text-muted-foreground">{receipt}</p>}
      {!message.deletedAt && !!visibleSessionSends?.length && <div className="flex flex-wrap gap-1 mt-1" aria-label="Session delivery">
        {visibleSessionSends.map((send) => <SessionSendChip key={send.sessionId} send={send} roomId={message.roomId} messageId={message.messageId} refreshKey={receiptRefreshKey} onGetSessionCommand={onGetSessionCommand} onRetrySessionSend={onRetrySessionSend} onCancelSessionSend={onCancelSessionSend} replied={replyCount > 0} />)}
      </div>}
      {reactionError && <p role="alert" className="text-sm text-err">Could not update reaction. Try again.</p>}
      {!message.deletedAt && !!message.attachments?.length && onDownloadAttachment && <RoomImageAttachments roomId={message.roomId} attachments={message.attachments} download={onDownloadAttachment} attachmentUrl={onAttachmentUrl} />}
      {!inThread && (replyCount > 0 || working) && onOpenThread && <Button type="button" variant="ghost" className="room-reply" disabled={!actionable}
        aria-label={`Reply in thread, ${threadLabel}`}
        onClick={(event) => onOpenThread(event.currentTarget)}>
        <span className="room-reply-avatars">{threadParticipants.slice(0, 4).map(member =>
          <ParticipantAvatar key={`${member.kind}:${member.id}`} participant={{ ...member, agentActivity: undefined }} size="xs" showPresence={false} />)}
          {threadParticipants.length > 4 && <span className="text-xs text-muted-foreground">+{threadParticipants.length - 4}</span>}
        </span>
        <span role={working ? "status" : undefined}>{threadLabel}</span>
      </Button>}
    </div>
  </article>;
}
