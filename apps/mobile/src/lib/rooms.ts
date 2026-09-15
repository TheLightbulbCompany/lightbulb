import { useAuth, useOrganization } from "@clerk/clerk-expo";
import {
  useRooms,
  type CanonicalRoomMessage,
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

const appActive = () => AppState.currentState === "active";
const onAppStateChange = (listener: () => void) => {
  const subscription = AppState.addEventListener("change", () => listener());
  return () => subscription.remove();
};

/**
 * The phone's half of RoomsPlatform: AppState. The SCREEN's own focus
 * (expo-router blurs a pushed screen without backgrounding the app) is the
 * shared hook's `visible` option — the read-marking rule is "on screen and in
 * the foreground", and on a phone those are two different facts.
 */
export function useRoomsPlatform(): RoomsPlatform {
  const { userId } = useAuth();
  const { organization } = useOrganization();
  const live = useLiveEvents();
  return useMemo(() => ({
    userId: userId ?? null,
    orgId: organization?.id ?? null,
    live,
    isVisible: appActive,
    onVisibilityChange: onAppStateChange,
    uuid: () => randomUUID(),
  }), [userId, organization?.id, live]);
}

/** The room machine, wired to this phone. */
export function useMobileRooms(options: UseRoomsOptions = {}): UseRoomsResult {
  return useRooms(useRoomsPlatform(), options);
}

/** One not-yet-canonical message a room screen still has to draw. */
export interface LocalMessageRow {
  message: CanonicalRoomMessage;
  /** The send failed; the row carries a retry affordance. */
  unsent: boolean;
}

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
  send: (draft: ComposedMessage) => Promise<void>;
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
    async (draft: ComposedMessage) => {
      if (!activeRoomId || !httpReady) return;
      // The composer resolves text, mentions and attachment ids; WHICH timeline
      // the message lands on is this hook's to say alone, because the composer
      // is the same component on both screens and cannot know which mounted it.
      const request: CreateMessageRequest = {
        ...draft,
        clientMessageId: randomUUID(),
        replyToMessageId: threadRootMessageId,
        threadRootMessageId,
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
      } catch {
        // sendState now reads rejected/uncertain, which is what draws the row.
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

/**
 * Authenticated image bytes → a data URI RN's <Image> can render. Same
 * FileReader rail teamsData.fetchAttachmentDataUri already uses.
 * ponytail: no cache; add an LRU keyed by attachmentId if scrolling a
 * photo-heavy room re-downloads noticeably.
 */
export function blobToDataUri(blob: Blob): Promise<string | null> {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onloadend = () => resolve(typeof reader.result === "string" ? reader.result : null);
    reader.onerror = () => resolve(null);
    reader.readAsDataURL(blob);
  });
}
