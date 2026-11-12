"use client";

// The member's DM with the assistant on the Lightbulb page: its messages and a plain composer, over the same rooms
// hook (and live room subscription) the old chat room used. No threads, tasks, panes or invites: talking is the orb.
import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { useRooms } from "@/hooks/useRooms";
import { Button } from "@/components/ui/button";
import { MarkdownView } from "./MarkdownView";

// ponytail: the newest page only (bounded history); add rooms.loadOlder on scroll-to-top when people ask for more.
export function AssistantThread({ roomId }: { roomId: string }) {
  const rooms = useRooms(null, true, true, false);
  const { selectRoom, sendMessage } = rooms;
  useEffect(() => { selectRoom(roomId); }, [roomId, selectRoom]);
  const [text, setText] = useState("");
  const [error, setError] = useState(false);
  const list = useRef<HTMLOListElement>(null);
  const open = rooms.activeRoomId === roomId;
  const messages = open ? rooms.messages : [];
  const pending = open ? rooms.optimisticMessages : []; // the hook drops each one when its canonical copy lands
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
      {messages.filter((m) => !m.deletedAt && m.text).map((m) => <li key={m.messageId} data-author={m.author.kind}>
        <b>{m.author.displayName}</b>
        <MarkdownView skipHtml>{m.text}</MarkdownView>
      </li>)}
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
