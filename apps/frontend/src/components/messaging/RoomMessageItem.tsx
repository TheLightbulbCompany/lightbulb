"use client";

import { useMemo, type ReactElement, type ReactNode } from "react";
import { MessageCircle } from "lucide-react";
import type { Components, UrlTransform } from "react-markdown";
import { sessionLabel, sessionStatus, specialistLabel, type CardReply, type LocalSession } from "@lightbulb/shared";
import { MessageRoutedTo, MessageTasks } from "@/components/tasks/MessageTasks";
import { useSuperAgent } from "@/hooks/useLiveEvents";
import { openMessagePanel } from "@/components/workspace/workspaceController";
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
import { CardContext, type CardContextValue, useCardPressState } from "./openui/CardContext";
import { RoomAppLinks } from "./RoomAppLinks";
import { RoomLinkPreviews } from "./RoomLinkPreviews";
import { RoomMessageContent } from "./RoomMessageContent";
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
  /** Posts a card's answer as the viewer (absent in read-only places: the controls disable). */
  onCardReply?: (reply: CardReply) => Promise<void>;
  /** The first human reply to this message, when it carries a card. */
  cardAnswer?: string | null;
  /** False in the feed: the fence stays a code block. */
  cardsEnabled?: boolean;
  /** The sessions this viewer can open: a referenced session resolves its live status here. */
  sessions?: readonly LocalSession[];
  onOpenSession?: (sessionId: string) => void;
  toolbarExtras?: ReactNode;
  menuExtras?: ReactNode;
  /** A person's colour in a session's chat (`personColor`), the same as in its terminal. */
  personColor?: string;
  /** Super-agent: open a thread pane (task cards, "Sent to …"). */
  onOpenTask?: (taskId: string) => void;
  /** Super-agent: reactions by agents (Lightbulb's 👀 "saw this") are a receipt, not a toggle. */
  isAgentId?: (id: string) => boolean;
}

/** A session chip is a reference, like a document link: it never messaged the session. */
function SessionCard({ sessionId, label, session, onOpen }: {
  sessionId: string; label?: string; session?: LocalSession; onOpen?: (sessionId: string) => void;
}) {
  const name = session ? sessionLabel(session) : label?.split(" · ").slice(0, -1).join(" · ") || "Session";
  // Open always routes by the stored id: a session that left the workspace list (ended more
  // than a week ago) still opens by id, and the detail view says so when access is really gone.
  return <span className="flex flex-wrap items-center gap-2 rounded-md border px-2 py-1 text-caption" data-session-card={sessionId}>
    <span className="font-medium">{name}</span>
    {session && <span className="text-muted-foreground">{sessionStatus[session.status]}</span>}
    {onOpen && <Button type="button" variant="ghost" size="sm" aria-label={`Open session ${name}`} onClick={() => onOpen(sessionId)}>Open session</Button>}
  </span>;
}

export function RoomMessageItem({ id, message, text, participant, currentUserId, actionable, inThread, collapsed, startEditing, onEditingClosed, replyCount, lastReplyAt, threadParticipants = [], workingNames = [], receipt, highlighted, mentionsViewer, markdown, urlTransform, quickReactions, reactionPending, reactionError, onToggleReaction, onOpenThread, onSelectParticipant, onUpdateMessage, onDownloadAttachment, onAttachmentUrl, onCardReply, cardAnswer = null, cardsEnabled = true, sessions = [], onOpenSession, toolbarExtras, menuExtras, personColor, onOpenTask, isAgentId }: RoomMessageItemProps): ReactElement {
  const superAgent = useSuperAgent();
  const cardReply = useMemo(() => onCardReply && actionable
    ? (replyText: string, clientMessageId: string) => onCardReply({ text: replyText, agentId: message.author.id, messageId: message.messageId, threadRootMessageId: message.threadRootMessageId, clientMessageId })
    : undefined, [onCardReply, actionable, message.author.id, message.messageId, message.threadRootMessageId]);
  // One press state per message (not per fence): a reply answers the message.
  const cardPress = useCardPressState(cardReply, cardAnswer);
  const cardContext = useMemo<CardContextValue | null>(() => !cardsEnabled || message.author.kind !== "agent" || message.deletedAt ? null : {
    messageId: message.messageId, roomId: message.roomId, authorId: message.author.id, threadRootMessageId: message.threadRootMessageId,
    attachments: message.attachments ?? [], ...cardPress, attachmentUrl: onAttachmentUrl,
  }, [message.messageId, message.roomId, message.author.kind, message.author.id, message.threadRootMessageId, message.deletedAt, message.attachments,
    cardPress, onAttachmentUrl, cardsEnabled]);
  const name = participant?.displayName || message.author.displayName || "Unknown author";
  const timestamp = !Number.isNaN(Date.parse(message.createdAt)) ? message.createdAt : undefined;
  const time = timestamp ? new Date(timestamp).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "";
  const working = workingNames.length ? `${workingNames.join(", ")} ${workingNames.length === 1 ? "is" : "are"} working…` : "";
  const replyLabel = replyCount > 0 ? `${replyCount} ${replyCount === 1 ? "reply" : "replies"}` : "";
  const threadLabel = [replyLabel, working || (lastReplyAt ? `last reply ${timeAgo(lastReplyAt)}` : "")].filter(Boolean).join(" · ");
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
  return <article id={id} tabIndex={-1} className={cn("room-message", collapsed && "room-message-collapsed", highlighted && "room-message-highlight", mentionsViewer && "room-message-mentions-me")} aria-label={`Message from ${name}`} data-person={personColor}>
    {collapsed
      ? <span className="room-message-gutter" aria-hidden="true">{time}</span>
      : participant ? <ParticipantAvatar participant={participant} showPresence={false} /> : <Identity name={name} avatarOnly size="default" />}
    <div className="room-message-content">
      {collapsed && timestamp && <time className="sr-only" dateTime={timestamp}>{time}</time>}
      {!collapsed && <div className="room-message-byline">{superAgent && message.specialist
        // A result posted for a specialist reads as the one front agent with its label (contracts-v2 §9).
        ? <span className="font-semibold truncate" data-specialist={message.specialist}>{specialistLabel(message.specialist)}</span>
        : participant
        ? <ParticipantProfile participant={participant} currentUserId={currentUserId} onMessage={onSelectParticipant} disabled={!actionable} />
        : <span className="font-semibold truncate" title={name}>{name}</span>}
        {message.author.kind === "agent" && <span className="room-agent-label">Agent</span>}
        {timestamp && <time className="text-ui text-muted-foreground" dateTime={timestamp} title={new Date(timestamp).toLocaleString()}>{time}</time>}
      </div>}
      <RoomMessageContent message={message} currentUserId={currentUserId} actionable={actionable} onUpdate={onUpdateMessage} startEditing={startEditing} onEditingClosed={onEditingClosed} actions={actions} menuExtras={menuExtras}>
        <CardContext.Provider value={cardContext}>
          <MarkdownView skipHtml components={markdown} urlTransform={urlTransform}>{text}</MarkdownView>
        </CardContext.Provider>
      </RoomMessageContent>
      {!message.deletedAt && !!message.sessionMentions?.length && <div className="flex flex-wrap gap-1 mt-1" aria-label="Referenced sessions">
        {message.sessionMentions.map((sessionId) => <SessionCard key={sessionId} sessionId={sessionId} label={message.sessionTargetLabels?.[sessionId]}
          session={sessions.find((item) => item.sessionId === sessionId)} onOpen={onOpenSession} />)}
      </div>}
      {!message.deletedAt && (superAgent && message.taskRefs?.length
        ? <MessageTasks message={message} viewerId={currentUserId} onOpen={onOpenTask} />
        : <MessageTaskCard text={message.text} prose={message.author.kind === "agent"} />)}
      {superAgent && message.forwardedFrom && <button type="button" className="block text-ui text-muted-foreground hover:text-foreground hover:underline"
        onClick={() => openMessagePanel(message.forwardedFrom!.roomId, message.forwardedFrom!.messageId)}>
        Forwarded from {message.forwardedFrom.roomName ? `#${message.forwardedFrom.roomName}` : "the conversation"} ›</button>}
      {!message.deletedAt && superAgent && message.routedTo && <MessageRoutedTo message={message} onOpen={onOpenTask}
        onShowReply={() => document.getElementById(id)?.parentElement?.querySelector(`#${CSS.escape(id)} ~ article`)?.scrollIntoView({ block: "nearest", behavior: "smooth" })} />}
      {!message.deletedAt && <RoomDocumentCards text={message.text} />}
      {!message.deletedAt && <RoomAppLinks text={message.text} />}
      {!message.deletedAt && <RoomLinkPreviews text={message.text} />}
      {!message.deletedAt && onToggleReaction && appliedReactions.length > 0 && <div className="flex flex-wrap gap-1 mt-1" aria-label="Message reactions">
        {appliedReactions.map(([emoji, users]) => {
          if (superAgent && isAgentId && users.every(isAgentId)) {
            return <span key={emoji} role="img" className="inline-flex h-8 items-center gap-1 px-3 text-ui text-muted-foreground" aria-label={emoji === "👀" ? "Lightbulb saw this" : `Lightbulb reacted ${emoji}`}>{emoji}</span>;
          }
          const selected = users.includes(currentUserId);
          return <Button key={emoji} type="button" size="sm" variant={selected ? "secondary" : "ghost"}
            aria-label={`${selected ? "Remove" : "Add"} ${emoji} reaction${users.length ? `, ${users.length}` : ""}`} aria-pressed={selected}
            disabled={!actionable || reactionPending} onClick={() => onToggleReaction(emoji)}>{emoji}<span>{users.length}</span></Button>;
        })}
      </div>}
      {receipt && <p role="status" className="text-caption text-muted-foreground">{receipt}</p>}
      {reactionError && <p role="alert" className="text-ui text-err">Could not update reaction. Try again.</p>}
      {!message.deletedAt && !!message.attachments?.length && onDownloadAttachment && <RoomImageAttachments roomId={message.roomId} attachments={message.attachments} download={onDownloadAttachment} attachmentUrl={onAttachmentUrl} />}
      {!inThread && (replyCount > 0 || working) && onOpenThread && <Button type="button" variant="ghost" className="room-reply" disabled={!actionable}
        aria-label={`Reply in thread, ${threadLabel}`}
        onClick={(event) => onOpenThread(event.currentTarget)}>
        <span className="room-reply-avatars">{threadParticipants.slice(0, 4).map(member =>
          <ParticipantAvatar key={`${member.kind}:${member.id}`} participant={{ ...member, agentActivity: undefined }} size="xs" showPresence={false} />)}
          {threadParticipants.length > 4 && <span className="text-caption text-muted-foreground">+{threadParticipants.length - 4}</span>}
        </span>
        <span role={working ? "status" : undefined}>{threadLabel}</span>
      </Button>}
    </div>
  </article>;
}
