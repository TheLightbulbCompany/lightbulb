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

/** The context of a card shown outside a room (a document): no `press`, so every control is disabled. */
export function readOnlyCardContext(messageId: string): CardContextValue {
  return { messageId, roomId: "", authorId: "", threadRootMessageId: null, attachments: [], answered: null, pending: false, error: null, retry: null };
}

export function useCardContext(): CardContextValue | null {
  return useContext(CardContext);
}

type OnReply = (text: string, clientMessageId: string) => Promise<void>;

type Attempt = { id: string; text: string; pending: boolean; error: string | null; sent: string | null };
const IDLE: Attempt = { id: "", text: "", pending: false, error: null, sent: null };
const ERR = "Couldn't send your reply. Try again.";

/** One press state per message: in flight, failed, or sent. `answer` is the room's reply when it has landed. */
export function useCardPressState(onReply: OnReply | undefined, answer: string | null): Pick<CardContextValue, "press" | "pending" | "error" | "answered" | "retry"> {
  // A send the server never answered may still have landed, and the server dedupes on
  // (user, clientMessageId) but refuses that id with a different body. So an unconfirmed
  // send is replayed AS IT WAS, same id and same text, whatever the next press carries
  // (`retry` tells the controls which one that is); if it had landed, the replay returns it.
  // A send the server rejected (an ApiError under 500) landed nothing: the id is dropped
  // and the next press may choose anything with a fresh one.
  const [state, setState] = useState<Attempt>(IDLE);
  const answered = answer ?? state.sent;
  // Once the canonical reply has landed, whatever this client still holds of the send goes: the local
  // marker, but also a pinned retry or an error from a POST that timed out after the server had committed
  // it. `answered` then follows the room: closed while the reply exists, open again if it is deleted.
  // (State adjusted during render on a prop change, the React-documented form; an effect would re-render once more.)
  const [seenAnswer, setSeenAnswer] = useState<string | null>(null);
  if (answer !== seenAnswer) {
    setSeenAnswer(answer);
    if (answer !== null && state !== IDLE) setState(IDLE);
  }
  const press = useCallback((text: string) => {
    if (!onReply || state.pending || answered !== null) return;
    const attempt = state.id ? { id: state.id, text: state.text } : { id: crypto.randomUUID(), text: text.trim() };
    if (!attempt.text) return;
    setState({ ...attempt, pending: true, error: null, sent: null });
    // A completion applies only to the attempt it belongs to: a canonical answer that arrived first has
    // already reset the state, and a late POST result must not write a stale marker back.
    const settle = (next: Attempt) => setState((cur) => (cur.id === attempt.id ? next : cur));
    onReply(attempt.text, attempt.id).then(
      () => settle({ ...IDLE, sent: attempt.text }),
      (e: unknown) => settle(e instanceof ApiError && e.status < 500 ? { ...IDLE, error: ERR } : { ...attempt, pending: false, error: ERR, sent: null }),
    );
  }, [onReply, state, answered]);
  const retry = !state.pending && state.id ? state.text : null;
  // One object per state change, so the message's context value (and every card under it) stays put across unrelated re-renders.
  return useMemo(() => ({ press: onReply ? press : undefined, pending: state.pending, error: state.error, answered, retry }), [onReply, press, state.pending, state.error, answered, retry]);
}
