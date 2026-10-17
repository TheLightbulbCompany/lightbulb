import { useAuth, useOrganization } from "@clerk/expo";
import {
  useRooms,
  cardReplyRequest,
  type CanonicalRoomMessage,
  type CardReply,
  type CreateMessageRequest,
  type OptimisticRoomMessage,
  type RoomsPlatform,
  type UseRoomsOptions,
  type UseRoomsResult,
} from "@lightbulb/shared";
import { randomUUID } from "expo-crypto";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AppState } from "react-native";

import { useLiveEvents } from "../providers/live-events";
import { roomsCache } from "./roomsCache";

/**
 * The phone's half of RoomsPlatform.
 *
 * `visible` is the SCREEN's own focus (expo-router blurs a pushed screen
 * without backgrounding the app), ANDed with AppState — the shared hook's
 * read-marking rule is "on screen and in the foreground", and on a phone
 * those are two different facts.
 */
export function useRoomsPlatform(visible = true): RoomsPlatform {
  const { userId } = useAuth();
  const { organization } = useOrganization();
  const live = useLiveEvents();
  const visibleRef = useRef(visible);
  visibleRef.current = visible;
  const isVisible = useCallback(
    () => visibleRef.current && AppState.currentState === "active",
    [],
  );
  const listeners = useRef(new Set<() => void>());
  const onVisibilityChange = useCallback((listener: () => void) => {
    listeners.current.add(listener);
    const subscription = AppState.addEventListener("change", () => listener());
    return () => {
      listeners.current.delete(listener);
      subscription.remove();
    };
  }, []);
  // The screen's own focus is the other half of isVisible(), so a change in it
  // has to notify too — AppState alone leaves a room that was blurred by a
  // pushed thread screen never re-marking read on the way back. Firing on
  // mount is a no-op: the shared hook subscribes in a later effect and calls
  // markVisibleRead() itself there, so the set is still empty here.
  useEffect(() => {
    for (const listener of listeners.current) listener();
  }, [visible]);
  return useMemo(() => ({
    userId: userId ?? null,
    orgId: organization?.id ?? null,
    live,
    isVisible,
    onVisibilityChange,
    uuid: () => randomUUID(),
  }), [userId, organization?.id, live, isVisible, onVisibilityChange]);
}

/** The room machine, wired to this phone. */
export function useMobileRooms(
  options: UseRoomsOptions & { visible?: boolean } = {},
): UseRoomsResult {
  const { visible = true, ...roomOptions } = options;
  // Persisted per user + workspace, so a cold start paints the last-known list.
  return useRooms(useRoomsPlatform(visible), { persistence: roomsCache, ...roomOptions });
}

/** One not-yet-canonical message a room screen still has to draw. */
export interface LocalMessageRow {
  message: CanonicalRoomMessage;
  /** The send failed; the row carries a retry affordance. */
  unsent: boolean;
}

/** A card's reply is its own message: its stable id, the card as the reply target, the card's own thread. */
export type SendOptions = Partial<Pick<CreateMessageRequest, "clientMessageId" | "replyToMessageId" | "threadRootMessageId">>;

/** The one builder for a card press: the shared request fields, sent through the room's own `send`. */
export const cardReplySender =
  (send: (draft: ComposedMessage, options?: SendOptions) => Promise<void>) =>
  (reply: CardReply): Promise<void> => {
    const { text, mentions, ...ids } = cardReplyRequest(reply);
    return send({ text, mentions }, ids);
  };

/** What a composer produces: the message minus every id only the send rail knows. */
export type ComposedMessage = Omit<
  CreateMessageRequest,
  "clientMessageId" | "replyToMessageId" | "threadRootMessageId"
>;

/**
 * Reports typing to `rooms.setTyping`, mirroring the web's
 * `MessagingWorkspaceContext.handleTyping` cadence exactly: report once when
 * typing starts (not on every keystroke — `rooms.setTyping` itself also
 * throttles the network call to 1/s), auto-stop after 3s of no further
 * keystroke, and report stopped right away when told to. `rooms.setTyping`
 * already resolves to a no-op with no active room.
 *
 * `focused` is the screen's own focus (the same flag each room screen already
 * tracks via `useFocusEffect`) ANDed against AppState here — a phone locked or
 * backgrounded mid-draft must not leave "typing…" showing forever for
 * everyone else, and a blurred screen (a pushed thread) must stop just as fast.
 */
const TYPING_STOP_MS = 3_000;

export function useTypingReport(
  setTyping: (isTyping: boolean) => void,
  focused: boolean,
): (typing: boolean) => void {
  const active = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const stop = useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
    if (active.current) setTyping(false);
    active.current = false;
  }, [setTyping]);

  const report = useCallback(
    (typing: boolean) => {
      if (timer.current) clearTimeout(timer.current);
      if (!typing) {
        if (active.current) setTyping(false);
        active.current = false;
        return;
      }
      if (!active.current) setTyping(true);
      active.current = true;
      timer.current = setTimeout(() => {
        active.current = false;
        setTyping(false);
      }, TYPING_STOP_MS);
    },
    [setTyping],
  );

  useEffect(() => {
    if (!focused) {
      stop();
      return;
    }
    const subscription = AppState.addEventListener("change", (state) => {
      if (state !== "active") stop();
    });
    return () => {
      subscription.remove();
      stop();
    };
  }, [focused, stop]);

  return report;
}

/**
 * Sending from one room screen, with failed sends kept on screen.
 *
 * The shared machine DROPS its optimistic row when a send fails (useRooms'
 * catch) and records the outcome in `sendState` only — so a screen that renders
 * `optimisticMessages` alone clears the composer and shows nothing at all.
 * `drafts` holds everything sent here until it persists, and `sendState` stays
 * the source of truth for which of them failed.
 *
 * `threadRootMessageId` is both what this screen sends into and the filter on
 * what it draws: `null` for a room's own timeline, the root's id for a thread.
 * A thread reply is also a reply TO the root, so it doubles as
 * `replyToMessageId`.
 *
 * ponytail: per-mount state, so navigating away loses the retry row (the
 * message is still absent server-side and the room list is unaffected). Slack
 * keeps failed sends across navigation; that needs the failed draft held
 * outside the screen — the same store draft persistence will want.
 */
export function useSendDrafts(
  rooms: UseRoomsResult,
  {
    authorId,
    authorName,
    threadRootMessageId = null,
  }: { authorId: string; authorName: string; threadRootMessageId?: string | null },
): {
  canSend: boolean;
  send: (draft: ComposedMessage, options?: SendOptions) => Promise<void>;
  retry: (clientMessageId: string) => Promise<void>;
  rows: LocalMessageRow[];
} {
  const { sendMessage, reconcileSend, sendState, activeRoomId, httpReady, optimisticMessages } =
    rooms;
  const [drafts, setDrafts] = useState<OptimisticRoomMessage[]>([]);
  const settled = useCallback(
    (clientMessageId: string) =>
      setDrafts((current) => current.filter((draft) => draft.clientMessageId !== clientMessageId)),
    [],
  );
  const send = useCallback(
    async (draft: ComposedMessage, options?: SendOptions) => {
      // A card press must hear the outcome (its retry rules read it), so it throws where a composer send stays quiet.
      if (!activeRoomId || !httpReady) {
        if (options) throw new Error("Conversation is not ready.");
        return;
      }
      // The composer resolves text, mentions and attachment ids; WHICH timeline
      // the message lands on is this hook's to say alone, because the composer
      // is the same component on both screens and cannot know which mounted it.
      const request: CreateMessageRequest = {
        ...draft,
        clientMessageId: randomUUID(),
        replyToMessageId: threadRootMessageId,
        threadRootMessageId,
        ...options,
      };
      // De-duped on clientMessageId, which also makes a constant-uuid test
      // environment safe.
      setDrafts((current) =>
        current.some((draft) => draft.clientMessageId === request.clientMessageId)
          ? current
          : [...current, { ...request, roomId: activeRoomId, createdAt: new Date().toISOString() }],
      );
      try {
        await sendMessage(request);
        settled(request.clientMessageId);
      } catch (error) {
        // sendState now reads rejected/uncertain, which is what draws the row.
        // A card press owns its own retry (the card keeps the id and replays it), so no second "Not sent" row.
        if (options) {
          settled(request.clientMessageId);
          throw error;
        }
      }
    },
    [activeRoomId, httpReady, threadRootMessageId, sendMessage, settled],
  );
  const retry = useCallback(
    async (clientMessageId: string) => {
      try {
        // The machine's own primitive: it reconciles history first and re-sends
        // the SAME request, which is the right handling of the uncertain case.
        await reconcileSend(clientMessageId);
        settled(clientMessageId);
      } catch {
        // sendState flips back and the row returns.
      }
    },
    [reconcileSend, settled],
  );
  const localMessage = useCallback(
    (message: OptimisticRoomMessage): CanonicalRoomMessage => ({
      messageId: message.clientMessageId,
      roomId: message.roomId,
      messageSeq: 0,
      roomRevision: 0,
      author: { kind: "human", id: authorId, displayName: authorName },
      text: message.text,
      mentions: message.mentions,
      replyToMessageId: message.replyToMessageId,
      threadRootMessageId: message.threadRootMessageId,
      createdAt: message.createdAt,
      runId: null,
    }),
    [authorId, authorName],
  );
  const mine = (message: OptimisticRoomMessage) =>
    message.threadRootMessageId === threadRootMessageId;
  // The two lists are disjoint by construction: the machine's `optimistic` holds
  // only in-flight sends. Plain derivations rather than memos, because
  // `sendState` is a fresh closure over the attempts map every render.
  const rows: LocalMessageRow[] = [
    ...optimisticMessages
      .filter(mine)
      .map((message) => ({ message: localMessage(message), unsent: false })),
    ...drafts
      .filter((draft) => {
        const state = sendState(draft.clientMessageId);
        return mine(draft) && (state === "rejected" || state === "uncertain");
      })
      .map((draft) => ({ message: localMessage(draft), unsent: true })),
  ];
  return { canSend: Boolean(activeRoomId) && httpReady, send, retry, rows };
}
