"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { PanelRightClose } from "lucide-react";
import { useAuth } from "@clerk/nextjs";
import { ApiError, personColor, type CreateMessageRequest, type RoomDetail } from "@lightbulb/shared";
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

/**
 * The session's chat (PR #3807): people talk beside the terminal,
 * never into it. Its own room slot (subscription, scroll, draft), so a terminal reset never touches
 * the draft. It posts only through the room message door; a chat seat never lets anyone type into
 * the session. Threads and reactions are left out: replies render inline.
 */
export function SessionChat({ sessionId, sessionOwnerId, knownRoomId, openChat, onClose, onRoom, onRead }: {
  sessionId: string;
  /** The chat room the session list already names for this reader: it loads beside the door's POST. */
  knownRoomId?: string;
  /** The session's own person: their name takes the owner colour, as in the terminal. */
  sessionOwnerId: string;
  openChat: (sessionId: string) => Promise<RoomDetail>;
  onClose: () => void;
  /** The chat room on screen, or null when the column closes (its notifications are not toasted while shown). */
  onRoom?: (roomId: string | null) => void;
  /** The reader's cursor in this chat moved (on open, and on each arrival read while it is open). */
  onRead?: () => void;
}) {
  const { userId } = useAuth();
  const rooms = useRooms(null, true, true, false, false);
  const { selectRoom, sendMessage } = rooms;
  // Read once: the list names the room after a first open, and that must not open the door again.
  const [known] = useState(knownRoomId ?? null);
  const [roomId, setRoomId] = useState<string | null>(known);
  // The door (a POST: seat, lifecycle, fence) must answer before anyone posts; reading need not wait.
  const [opened, setOpened] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    if (known) selectRoom(known);
    openChat(sessionId).then((room) => {
      if (!alive) return;
      setRoomId(room.roomId);
      setOpened(true);
      selectRoom(room.roomId);
      onRoom?.(room.roomId);
    }, (e: unknown) => {
      if (alive) setError(e instanceof ApiError && e.status === 409 ? "This chat is full." : "Chat is not available for this session.");
    });
    return () => { alive = false; onRoom?.(null); };
  }, [sessionId, known, openChat, selectRoom, onRoom]);

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
    } catch (e) {
      if (!textRef.current.trim()) {
        setText(request.text);
        setClientMessageId(request.clientMessageId);
      }
      throw e;
    }
  }, [sendMessage]);

  const room = roomId && rooms.activeRoom?.roomId === roomId ? rooms.activeRoom : undefined;
  const members = (room?.members ?? []) as RoomParticipant[];
  const messages = roomId && rooms.activeRoomId === roomId ? rooms.messages : [];
  const archived = Boolean(room?.archivedAt);
  // The header count and the sidebar row are the session list's: it is read again once the cursor
  // has moved, never before (the refetch a new message triggers races the read and still counts it).
  const lastReadSeq = room?.lastReadSeq;
  useEffect(() => { if (lastReadSeq !== undefined) onRead?.(); }, [lastReadSeq, onRead]);
  const me = userId ?? "";
  const renderMessage = (message: RoomMessage, collapsed: boolean) => <RoomMessageItem key={message.messageId}
    id={`session-chat-${message.messageId}`} message={message} text={message.text}
    participant={members.find((m) => m.kind === message.author.kind && m.id === message.author.id)}
    currentUserId={me} actionable={!archived} inThread={false} collapsed={collapsed} replyCount={0} highlighted={false}
    mentionsViewer={message.mentions.some((m) => m.kind === "human" && m.id === me)}
    markdown={roomMarkdown} urlTransform={roomUrlTransform} quickReactions={NO_REACTIONS} reactionPending={false} reactionError={false}
    onDownloadAttachment={rooms.downloadAttachment} onAttachmentUrl={rooms.attachmentUrl}
    personColor={message.author.kind === "human" ? personColor(message.author.id, sessionOwnerId) : undefined} />;

  return <aside className="session-chat" aria-label="Session chat">
    <header className="room-context-header">
      {/* On the left, and not an X: the session's own close sits at the right end of its header, where this one used to land. */}
      <Button variant="ghost" size="icon-sm" aria-label="Close chat" title="Close chat" onClick={onClose}><PanelRightClose aria-hidden="true" /></Button>
      <h3 className="font-semibold">Chat</h3>
    </header>
    {rooms.accessRemoved
      // Sharing paused (or the reader left the workspace) while the column was open: the room panel's words.
      ? <p role="status" className="p-4 text-sm text-muted-foreground">Access removed. You can no longer view this conversation.</p>
      : error ? <p role="status" className="p-4 text-sm text-muted-foreground">{error}</p> : <>
      <RoomTimeline roomId={roomId ?? "opening"} messages={messages} readSeq={null} ownLastSentSeq={null}
        renderMessage={renderMessage} historyComplete={rooms.historyComplete} historyLoading={rooms.historyLoading}
        historyError={rooms.historyErrorMessage} onLoadOlder={() => void rooms.loadOlder()}
        empty={<p className="room-empty">{opened ? "Talk about the session here. The terminal stays the session's." : "Opening chat…"}</p>}>
        {rooms.optimisticMessages.filter((m) => m.roomId === roomId).map((m) =>
          <p key={m.clientMessageId} className="room-message opacity-60">{m.text}</p>)}
      </RoomTimeline>
      {archived
        ? <p role="status" className="room-composer room-archived text-sm text-muted-foreground">This session ended. Its chat is read-only.</p>
        : <RoomComposer text={text} onTextChange={setText} members={members} clientMessageId={clientMessageId}
          sendState={rooms.sendState(clientMessageId)} onSubmit={send} onRetry={send}
          onReconcile={(id) => rooms.reconcileSend(id)} onUploadAttachment={rooms.uploadAttachment} disabled={!opened || !rooms.httpReady} label="Message the people here" placeholder="Message the people here" roomKind="channel" />}
    </>}
  </aside>;
}
