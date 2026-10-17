import { ApiError, type MessageAttachment } from "@lightbulb/shared";
import { randomUUID } from "expo-crypto";
import { createContext, useCallback, useContext, useMemo, useRef, useState } from "react";

/**
 * The phone twin of the web CardContext: what a card needs from the message it sits in.
 * Provided by MessageGroup for an AGENT's message only, so a person's pasted fence is
 * plain code. The press state is the MESSAGE's, not the fence's.
 */
export interface CardReplyValue {
  messageId: string;
  roomId: string;
  authorId: string;
  threadRootMessageId: string | null;
  attachments: readonly MessageAttachment[];
  /** The person's answer once one exists (the room's, or the one just sent). */
  answered: string | null;
  /** Posts `text` as the person in reply to this message. Absent when read-only. */
  press?: (text: string) => void;
  /** A press is in flight: every control in the message waits. */
  pending: boolean;
  /** The last press failed; the controls are open again for a retry. */
  error: string | null;
  /** The reply of a send that got no answer: the next press replays it, the other options wait. */
  retry: string | null;
  attachmentUrl?: (roomId: string, attachmentId: string) => Promise<{ url: string }>;
}

export const CardReplyContext = createContext<CardReplyValue | null>(null);
export const useCardReply = () => useContext(CardReplyContext);

export type OnReply = (text: string, clientMessageId: string) => Promise<void>;

/**
 * One press state per message: in flight, failed, or sent. `answer` is the room's reply once
 * it has landed. Same rules as the web's useCardPressState: an unconfirmed send is replayed
 * unchanged (same id, same text, other options wait); a rejected one (ApiError < 500) frees the id.
 */
export function useCardPressState(
  onReply: OnReply | undefined,
  answer: string | null,
): Pick<CardReplyValue, "press" | "pending" | "error" | "answered" | "retry"> {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState<string | null>(null);
  const [retry, setRetry] = useState<string | null>(null);
  const answered = answer ?? sent;
  const attempt = useRef<{ id: string; text: string } | null>(null);
  const press = useCallback((text: string) => {
    if (!onReply || pending || answered !== null) return;
    const current = attempt.current ?? { id: randomUUID(), text: text.trim() };
    if (!current.text) return;
    attempt.current = current;
    setPending(true);
    setError(null);
    onReply(current.text, current.id).then(() => {
      attempt.current = null;
      setRetry(null);
      setSent(current.text);
    }, (e: unknown) => {
      if (e instanceof ApiError && e.status < 500) {
        attempt.current = null;
        setRetry(null);
      } else {
        setRetry(current.text);
      }
      setError("Couldn't send your reply. Try again.");
    }).finally(() => setPending(false));
  }, [onReply, pending, answered]);
  return useMemo(() => ({ press: onReply ? press : undefined, pending, error, answered, retry }), [onReply, press, pending, error, answered, retry]);
}
