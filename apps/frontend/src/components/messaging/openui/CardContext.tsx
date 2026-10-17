"use client";

import { createContext, useCallback, useContext, useMemo, useState } from "react";
import { ApiError, type MessageAttachment } from "@lightbulb/shared";

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
  /** The reply of a send that got no answer. It may have landed, so the next press replays it with the same id and the other options wait. */
  retry: string | null;
  attachmentUrl?: (roomId: string, attachmentId: string) => Promise<{ url: string }>;
}

export const CardContext = createContext<CardContextValue | null>(null);

export function useCardContext(): CardContextValue | null {
  return useContext(CardContext);
}

type OnReply = (text: string, clientMessageId: string) => Promise<void>;

/** One press state per message: in flight, failed, or sent. `answer` is the room's reply when it has landed. */
export function useCardPressState(onReply: OnReply | undefined, answer: string | null): Pick<CardContextValue, "press" | "pending" | "error" | "answered" | "retry"> {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState<string | null>(null);
  const [retry, setRetry] = useState<string | null>(null);
  const answered = answer ?? sent;
  // A send the server never answered may still have landed, and the server dedupes on
  // (user, clientMessageId) but refuses that id with a different body. So an unconfirmed
  // send is replayed AS IT WAS, same id and same text, whatever the next press carries
  // (`retry` tells the controls which one that is); if it had landed, the replay returns it.
  // A send the server rejected (an ApiError under 500) landed nothing: the id is dropped
  // and the next press may choose anything with a fresh one.
  const [attempt, setAttempt] = useState<{ id: string; text: string } | null>(null);
  // Once the canonical reply has landed, whatever this client still holds of the send goes: the local
  // marker, but also a pinned retry or an error from a POST that timed out after the server had committed
  // it. `answered` then follows the room: closed while the reply exists, open again if it is deleted.
  // (State adjusted during render on a prop change, the React-documented form; an effect would re-render once more.)
  const [seenAnswer, setSeenAnswer] = useState<string | null>(null);
  if (answer !== seenAnswer) {
    setSeenAnswer(answer);
    if (answer !== null) {
      setAttempt(null);
      setSent(null);
      setRetry(null);
      setError(null);
    }
  }
  const press = useCallback((text: string) => {
    if (!onReply || pending || answered !== null) return;
    const current = attempt ?? { id: crypto.randomUUID(), text: text.trim() };
    if (!current.text) return;
    setAttempt(current);
    setPending(true);
    setError(null);
    onReply(current.text, current.id).then(() => {
      setAttempt(null);
      setRetry(null);
      setSent(current.text);
    }, (e: unknown) => {
      if (e instanceof ApiError && e.status < 500) {
        setAttempt(null);
        setRetry(null);
      } else {
        setRetry(current.text);
      }
      setError("Couldn't send your reply. Try again.");
    }).finally(() => setPending(false));
  }, [onReply, pending, answered, attempt]);
  // One object per state change, so the message's context value (and every card under it) stays put across unrelated re-renders.
  return useMemo(() => ({ press: onReply ? press : undefined, pending, error, answered, retry }), [onReply, press, pending, error, answered, retry]);
}
