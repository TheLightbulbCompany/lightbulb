import { ApiError, type MessageAttachment } from "@lightbulb/shared";
import { randomUUID } from "expo-crypto";
import { createContext, useCallback, useContext, useEffect, useMemo, useSyncExternalStore } from "react";

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
  attachmentUrl?: (roomId: string, attachmentId: string) => Promise<{ url: string; expiresIn: number }>;
}

export const CardReplyContext = createContext<CardReplyValue | null>(null);
export const useCardReply = () => useContext(CardReplyContext);

export type OnReply = (text: string, clientMessageId: string) => Promise<void>;

const ERR = "Couldn't send your reply. Try again.";

/** `id === ""` means no attempt is pinned (idle, or the server rejected the last one). */
type Attempt = { id: string; text: string; pending: boolean; error: string | null; sent: string | null };
const IDLE: Attempt = { id: "", text: "", pending: false, error: null, sent: null };

// The press state lives ABOVE the row: the room list is a FlatList, so a row that scrolls out of
// the window unmounts, and a pinned attempt kept in the row would die with it. Scrolling back and
// pressing would then mint a new id while the first send may have committed. Keyed by messageId.
const attempts = new Map<string, Attempt>();
const listeners = new Map<string, Set<() => void>>();
const read = (messageId: string): Attempt => attempts.get(messageId) ?? IDLE;
const write = (messageId: string, next: Attempt) => {
  if (next === IDLE) attempts.delete(messageId);
  else attempts.set(messageId, next);
  listeners.get(messageId)?.forEach((cb) => cb());
};
const subscribe = (messageId: string, cb: () => void) => {
  const set = listeners.get(messageId) ?? new Set<() => void>();
  set.add(cb);
  listeners.set(messageId, set);
  // Every MessageGroup subscribes, and FlatList rows come and go: drop the bucket with its last listener.
  return () => { set.delete(cb); if (set.size === 0 && listeners.get(messageId) === set) listeners.delete(messageId); };
};
export function _resetCardAttemptsForTests() {
  attempts.clear();
  listeners.clear();
}

/**
 * One press state per message: in flight, failed, or sent. `answer` is the room's reply once
 * it has landed. Same rules as the web: an unconfirmed send is replayed unchanged (same id, same
 * text, other options wait); a rejected one (ApiError < 500) frees the id.
 */
export function useCardPressState(
  messageId: string,
  viewerId: string,
  onReply: OnReply | undefined,
  answer: string | null,
): Pick<CardReplyValue, "press" | "pending" | "error" | "answered" | "retry"> {
  // Keyed by (viewer, message): a pinned attempt belongs to the person who pressed it, so after a
  // sign-out the next member in this app process starts clean instead of replaying their text and id.
  const key = `${viewerId}\u0000${messageId}`;
  const state = useSyncExternalStore(
    useCallback((cb: () => void) => subscribe(key, cb), [key]),
    () => read(key),
  );
  const answered = answer ?? state.sent;
  // Once the canonical reply has landed, `answered` follows it (closed while it exists, open again if it is
  // deleted) and whatever this client still holds of the send goes: the local marker, but also a pinned
  // retry or an error from a POST that timed out after the server had committed it.
  useEffect(() => {
    if (answer !== null && state !== IDLE) write(key, IDLE);
  }, [answer, state, key]);
  const press = useCallback((text: string) => {
    // Read the store, not the rendered snapshot: two presses in one frame both see the idle render,
    // and the second must find the first's attempt already written rather than mint a second id.
    const cur = read(key);
    if (!onReply || cur.pending || (answer ?? cur.sent) !== null) return;
    const attempt = cur.id ? { id: cur.id, text: cur.text } : { id: randomUUID(), text: text.trim() };
    if (!attempt.text) return;
    write(key, { ...attempt, pending: true, error: null, sent: null });
    // A completion applies only to the attempt it belongs to: a canonical answer that arrived first has
    // already cleared the entry, and a late POST result must not write a stale marker back.
    const settle = (next: Attempt) => { if (read(key).id === attempt.id) write(key, next); };
    onReply(attempt.text, attempt.id).then(
      () => settle({ ...IDLE, sent: attempt.text }),
      (e: unknown) => settle(e instanceof ApiError && e.status < 500
        ? { ...IDLE, error: ERR }
        : { ...attempt, pending: false, error: ERR, sent: null }),
    );
  }, [key, onReply, answer]);
  const retry = !state.pending && state.id ? state.text : null;
  return useMemo(
    () => ({ press: onReply ? press : undefined, pending: state.pending, error: state.error, answered, retry }),
    [onReply, press, state.pending, state.error, answered, retry],
  );
}
