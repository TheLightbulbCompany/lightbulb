"use client";

import { createContext, useCallback, useContext, useMemo, useRef, useState } from "react";
import type { MessageAttachment } from "@lightbulb/shared";

/**
 * What a card needs from the message it sits in. Provided by RoomMessageItem for an
 * AGENT's message only: without a provider a fence is plain code (a person's paste can
 * never become live buttons), and a card shown outside a room (a session transcript, a
 * document) renders read-only.
 *
 * The press state is the MESSAGE's, not the fence's: a reply answers the message, so a
 * reply that carries two fences (against the one-card rule) still takes one answer.
 */
export interface CardContextValue {
  messageId: string;
  roomId: string;
  authorId: string;
  threadRootMessageId: string | null;
  attachments: readonly MessageAttachment[];
  /** The person's answer once one exists (the room's, or the one just sent): controls disable and show it. */
  answered: string | null;
  /** Posts `text` as the person in reply to this message. Absent in a read-only place. */
  press?: (text: string) => void;
  /** A press is in flight: every control in the message waits. */
  pending: boolean;
  /** The last press failed; the controls are open again for a retry. */
  error: string | null;
  attachmentUrl?: (roomId: string, attachmentId: string) => Promise<{ url: string }>;
}

export const CardContext = createContext<CardContextValue | null>(null);

export function useCardContext(): CardContextValue | null {
  return useContext(CardContext);
}

type OnReply = (text: string, clientMessageId: string) => Promise<void>;

/** One press state per message: in flight, failed, or sent. `answer` is the room's reply when it has landed. */
export function useCardPressState(onReply: OnReply | undefined, answer: string | null): Pick<CardContextValue, "press" | "pending" | "error" | "answered"> {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState<string | null>(null);
  const answered = answer ?? sent;
  // ONE clientMessageId per message until a send is confirmed, whatever text each press carries:
  // a failed POST may still have committed, and the server dedupes on (user, clientMessageId),
  // so a different choice after a failure must replay the same id rather than post a second answer.
  // If the first did commit, the replay returns it and the room's reply then shows the real answer.
  const clientId = useRef<string | null>(null);
  const press = useCallback((text: string) => {
    const reply = text.trim();
    if (!onReply || pending || answered !== null || !reply) return;
    const clientMessageId = clientId.current ?? crypto.randomUUID();
    clientId.current = clientMessageId;
    setPending(true);
    setError(null);
    onReply(reply, clientMessageId).then(() => {
      clientId.current = null;
      setSent(reply);
    }, () => setError("Couldn't send your reply. Try again.")).finally(() => setPending(false));
  }, [onReply, pending, answered]);
  // One object per state change, so the message's context value (and every card under it) stays put across unrelated re-renders.
  return useMemo(() => ({ press: onReply ? press : undefined, pending, error, answered }), [onReply, press, pending, error, answered]);
}
