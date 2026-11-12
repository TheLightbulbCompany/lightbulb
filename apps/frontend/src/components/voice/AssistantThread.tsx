"use client";

// The member's DM with the assistant on the Lightbulb page: its messages and a plain composer, over the same rooms
// hook (and live room subscription) the old chat room used. No threads, tasks, panes or invites: talking is the orb.
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { cardAnswersFor, cardReplyRequest, type CanonicalRoomMessage, type CreateMessageRequest } from "@lightbulb/shared";
import { useRooms } from "@/hooks/useRooms";
import { Button } from "@/components/ui/button";
import { AutomationConfirmCard } from "@/components/tasks/AutomationConfirmCard";
import { MarkdownView } from "./MarkdownView";
import { cardMarkdown } from "./cardMarkdown";
import { CardContext, useCardPressState, type CardContextValue } from "./openui/CardContext";
import { AttachmentLink, TaskResults } from "./TaskResults";

const NONE: CanonicalRoomMessage[] = [];

// ponytail: the newest page only (bounded history); add rooms.loadOlder on scroll-to-top when people ask for more.
export function AssistantThread({ roomId }: { roomId: string }) {
  const rooms = useRooms(null, true, true, false);
  const { selectRoom, sendMessage } = rooms;
  useEffect(() => { selectRoom(roomId); }, [roomId, selectRoom]);
  const [text, setText] = useState("");
  const [error, setError] = useState(false);
  const list = useRef<HTMLOListElement>(null);
  const open = rooms.activeRoomId === roomId;
  const messages = open ? rooms.messages : NONE;
  const pending = open ? rooms.optimisticMessages : []; // the hook drops each one when its canonical copy lands
  const answers = useMemo(() => cardAnswersFor(messages), [messages]);
  const replying = open && rooms.runs.some((run) => run.roomId === roomId && run.state === "running");
  useEffect(() => { if (list.current) list.current.scrollTop = list.current.scrollHeight; }, [messages.length, pending.length, replying]);
  // An uncertain send retried with the same text reuses its id, so the server's idempotency receipt dedupes it.
  const failed = useRef<{ text: string; id: string } | null>(null);

  async function send(e?: FormEvent) {
    e?.preventDefault();
    const body = text.trim();
    if (!body || !open) return;
    setText("");
    setError(false);
    const clientMessageId = failed.current?.text === body ? failed.current.id : crypto.randomUUID();
    try {
      await sendMessage({ clientMessageId, text: body, mentions: [], replyToMessageId: null, threadRootMessageId: null });
      failed.current = null;
      // A New chat is titled by its first message (best effort: the list says "New chat" until then).
      if (rooms.activeRoom?.kind === "group-dm" && !rooms.activeRoom.name && messages.length === 0)
        void rooms.updateRoom({ name: body.slice(0, 80) }).catch(() => undefined);
    } catch {
      failed.current = { text: body, id: clientMessageId };
      setError(true);
      setText((current) => current || body);
    }
  }
  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) void send(e);
  }

  if (!open) return <p className="vx-empty" role="status">Loading chat…</p>;
  return <section className="vx-thread" aria-label="Conversation">
    <ol ref={list}>
      {messages.filter((m) => !m.deletedAt && (m.text || m.taskRefs?.length || m.attachments?.length)).map((m) => <Message key={m.messageId} message={m}
        answer={answers.get(m.messageId) ?? null} send={sendMessage} attachmentUrl={rooms.attachmentUrl} />)}
      {pending.map((p) => <li key={p.clientMessageId} data-author="human" data-pending>
        <MarkdownView skipHtml>{p.text}</MarkdownView>
      </li>)}
      {replying && <li data-pending role="status">Lightbulb is replying…</li>}
    </ol>
    <form onSubmit={send}>
      <textarea value={text} onChange={(e) => setText(e.target.value)} onKeyDown={onKeyDown} rows={2} className="vx-field"
        placeholder="Message Lightbulb" aria-label="Message" />
      <Button type="submit" size="sm" disabled={!text.trim()}>Send</Button>
    </form>
    {error && <p className="vx-thread-error" role="alert">Not sent. Try again.</p>}
  </section>;
}

/** One message. An agent's message is a card's home: its fences draw live controls, and a press posts the answer as a
 *  reply to this message (`replyToMessageId`), which is how the card's author knows it was answered. */
function Message({ message: m, answer, send, attachmentUrl }: {
  message: CanonicalRoomMessage; answer: string | null;
  send: (request: CreateMessageRequest) => Promise<unknown>;
  attachmentUrl: CardContextValue["attachmentUrl"];
}) {
  const agent = m.author.kind === "agent";
  const reply = useCallback((text: string, clientMessageId: string) => send(cardReplyRequest({
    text, agentId: m.author.id, messageId: m.messageId, threadRootMessageId: m.threadRootMessageId, clientMessageId,
  })).then(() => undefined), [send, m.author.id, m.messageId, m.threadRootMessageId]);
  const press = useCardPressState(agent ? reply : undefined, answer);
  const card = useMemo<CardContextValue | null>(() => agent ? {
    messageId: m.messageId, roomId: m.roomId, authorId: m.author.id, threadRootMessageId: m.threadRootMessageId,
    attachments: m.attachments ?? [], ...press, attachmentUrl,
  } : null, [agent, m.messageId, m.roomId, m.author.id, m.threadRootMessageId, m.attachments, press, attachmentUrl]);
  return <li data-author={m.author.kind}>
    <b>{m.author.displayName}</b>
    <CardContext.Provider value={card}><MarkdownView skipHtml components={cardMarkdown}>{m.text}</MarkdownView></CardContext.Provider>
    {!!m.attachments?.length && <ul className="vx-task-outputs" aria-label="Attachments">{m.attachments.map((a) =>
      <li key={a.id}><AttachmentLink roomId={m.roomId} attachmentId={a.id} name={a.fileName} /></li>)}</ul>}
    <TaskResults message={m} />
    {m.kind === "automation_confirm" && m.automationId && <AutomationConfirmCard automationId={m.automationId} />}
  </li>;
}
