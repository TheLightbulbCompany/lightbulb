"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { PanelRightClose } from "lucide-react";
import { useAuth } from "@clerk/nextjs";
import { ApiError, cardAnswersFor, cardReplyRequest, personColor, type CardReply, type CreateMessageRequest, type RoomDetail } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { useRooms } from "@/hooks/useRooms";
import { roomUrlTransform } from "./markdown";
import { roomMarkdown } from "./roomMarkdown";
import { RoomComposer } from "./RoomComposer";
import { RoomMessageItem } from "./RoomMessageItem";
import { RoomTimeline } from "./RoomTimeline";
import type { RoomMessage, RoomParticipant } from "./types";
import "./messaging.css";

const NO_REACTIONS: readonly string[] = [];
const NO_MESSAGES: readonly RoomMessage[] = [];

/** The column's words; Canvas's app conversation (AppConversation) says them about an app. */
export type ChatWords = { label: string; title: string; empty: string; unavailable: string; full: string; ended: string; compose?: string };
const SESSION_WORDS: ChatWords = {
  label: "Session chat", title: "Chat", empty: "Talk about the session here. The terminal stays the session's.",
  unavailable: "Chat is not available for this session.", full: "This chat is full.", ended: "This session ended. Its chat is read-only.",
};

/**
 * The session's chat (PR #3807): people talk beside the terminal,
 * never into it. Its own room slot (subscription, scroll, draft), so a terminal reset never touches
 * the draft. It posts only through the room message door; a chat seat never lets anyone type into
 * the session. Threads and reactions are left out: replies render inline.
 */
export function SessionChat({ sessionId, sessionOwnerId, openChat, onClose, onRoom, onRead, onSend, readOnly = false, words = SESSION_WORDS }: {
  /** What openChat opens: a session, or an app in Canvas. */
  sessionId: string;
  /** The session's own person: their name takes the owner colour, as in the terminal. */
  sessionOwnerId: string;
  openChat: (sessionId: string) => Promise<Pick<RoomDetail, "roomId">>;
  /** Absent: the column has no close (Canvas's app conversation stays beside the preview). */
  onClose?: () => void;
  /** The chat room on screen, or null when the column closes (its notifications are not toasted while shown). */
  onRoom?: (roomId: string | null) => void;
  /** The reader's cursor in this chat moved (on open, and on each arrival read while it is open). */
  onRead?: () => void;
  words?: ChatWords;
  /** A message went out from this column's composer. */
  onSend?: () => void;
  /** Look, don't act: no composer and no live card buttons (a request's progress, steered from its room's composer). */
  readOnly?: boolean;
}) {
  const { userId } = useAuth();
  const rooms = useRooms(null, true, true, false);
  const { selectRoom, sendMessage } = rooms;
  // Nothing is selected until the door (a POST: seat, lifecycle, fence) answers, and then only the
  // room it returns. A room id the session list already names is never loaded ahead of it: that id
  // can be stale, and a refused read cannot be undone by a door that then opens.
  const [roomId, setRoomId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    openChat(sessionId).then((room) => {
      if (!alive) return;
      setRoomId(room.roomId);
      selectRoom(room.roomId);
      onRoom?.(room.roomId);
    }, (e: unknown) => {
      if (alive) setError(e instanceof ApiError && e.status === 409 ? words.full : words.unavailable);
    });
    return () => { alive = false; onRoom?.(null); };
  }, [sessionId, openChat, selectRoom, onRoom, words]);

  // Sharing paused while the column was open: the hook already dropped the subscription and stopped
  // reading; the "Access removed" panel is not the room on screen either.
  const accessRemoved = rooms.accessRemoved;
  useEffect(() => { if (accessRemoved) onRoom?.(null); }, [accessRemoved, onRoom]);

  const [text, setText] = useState("");
  const [clientMessageId, setClientMessageId] = useState(() => crypto.randomUUID());
  const textRef = useRef(text);
  useEffect(() => { textRef.current = text; }, [text]);
  const send = useCallback(async (request: CreateMessageRequest) => {
    // The box empties now; a failed send gives its text back if the box is still empty.
    setText("");
    setClientMessageId(crypto.randomUUID());
    try {
      await sendMessage(request);
      onSend?.();
    } catch (e) {
      if (!textRef.current.trim()) {
        setText(request.text);
        setClientMessageId(request.clientMessageId);
      }
      throw e;
    }
  }, [sendMessage, onSend]);

  const room = roomId && rooms.activeRoom?.roomId === roomId ? rooms.activeRoom : undefined;
  const members = (room?.members ?? []) as RoomParticipant[];
  const messages = roomId && rooms.activeRoomId === roomId ? rooms.messages : NO_MESSAGES;
  // An agent's card answers here as in a room: the press posts as the person, addressed to the agent, under the card.
  const onCardReply = useCallback(async (reply: CardReply) => { await sendMessage(cardReplyRequest(reply)); }, [sendMessage]);
  const cardAnswers = useMemo(() => cardAnswersFor(messages), [messages]);
  const archived = Boolean(room?.archivedAt);
  // The header count and the sidebar row are the session list's: it is read again once the cursor
  // has moved, never before (the refetch a new message triggers races the read and still counts it).
  const lastReadSeq = room?.lastReadSeq;
  useEffect(() => { if (lastReadSeq !== undefined) onRead?.(); }, [lastReadSeq, onRead]);
  const me = userId ?? "";
  const renderMessage = (message: RoomMessage, collapsed: boolean) => {
    const participant = members.find((m) => m.kind === message.author.kind && m.id === message.author.id);
    return <RoomMessageItem key={message.messageId}
    id={`session-chat-${message.messageId}`} message={message} text={message.text} participant={participant}
    onCardReply={participant && !readOnly ? onCardReply : undefined} cardAnswer={cardAnswers.get(message.messageId) ?? null}
    currentUserId={me} actionable={!archived && !readOnly} inThread={false} collapsed={collapsed} replyCount={0} highlighted={false}
    mentionsViewer={message.mentions.some((m) => m.kind === "human" && m.id === me)}
    markdown={roomMarkdown} urlTransform={roomUrlTransform} quickReactions={NO_REACTIONS} reactionPending={false} reactionError={false}
    onDownloadAttachment={rooms.downloadAttachment} onAttachmentUrl={rooms.attachmentUrl}
    personColor={message.author.kind === "human" ? personColor(message.author.id, sessionOwnerId) : undefined} />;
  };

  return <aside className="session-chat" aria-label={words.label}>
    <header className="room-context-header">
      {/* On the left, and not an X: the session's own close sits at the right end of its header, where this one used to land. */}
      {onClose && <Button variant="ghost" size="icon-sm" aria-label="Close chat" title="Close chat" onClick={onClose}><PanelRightClose aria-hidden="true" /></Button>}
      <h3 className="font-semibold">{words.title}</h3>
    </header>
    {rooms.accessRemoved
      // Sharing paused (or the reader left the workspace) while the column was open: the room panel's words.
      ? <p role="status" className="p-4 text-ui text-muted-foreground">Access removed. You can no longer view this conversation.</p>
      : error ? <p role="status" className="p-4 text-ui text-muted-foreground">{error}</p> : <>
      <RoomTimeline roomId={roomId ?? "opening"} messages={messages} readSeq={null} ownLastSentSeq={null}
        renderMessage={renderMessage} historyComplete={rooms.historyComplete} historyLoading={rooms.historyLoading}
        historyError={rooms.historyErrorMessage} onLoadOlder={() => void rooms.loadOlder()}
        empty={<p className="room-empty">{roomId ? words.empty : "Opening chat…"}</p>}>
        {rooms.optimisticMessages.filter((m) => m.roomId === roomId).map((m) =>
          <p key={m.clientMessageId} className="room-message opacity-60">{m.text}</p>)}
      </RoomTimeline>
      {readOnly ? null : archived
        ? <p role="status" className="room-composer room-archived text-ui text-muted-foreground">{words.ended}</p>
        : <RoomComposer text={text} onTextChange={setText} members={members} clientMessageId={clientMessageId}
          sendState={rooms.sendState(clientMessageId)} onSubmit={send} onRetry={send}
          onReconcile={(id) => rooms.reconcileSend(id)} onUploadAttachment={rooms.uploadAttachment} disabled={!roomId || !rooms.httpReady} label={words.compose ?? "Message the people here"} placeholder={words.compose ?? "Message the people here"} roomKind="channel" />}
    </>}
  </aside>;
}
