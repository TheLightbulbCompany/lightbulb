/**
 * The room state machine shared by the web and mobile clients: room list,
 * active-room detail, history reconciliation, optimistic sends, typing, live
 * runs, and read marking. Every platform-specific capability it needs —
 * identity, the live-events context value, visibility, uuid minting — arrives
 * through `RoomsPlatform`; there is no DOM global in here.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import useSWR from "swr";
import { ApiError } from "../api/error";
import type { LiveEvent } from "../live/types";
import type { MessagingClient, RoomAttachmentInput } from "./client";
import { applyRunFrame, lastSequence, mergeCanonicalMessages, type LiveRoomRun } from "./timeline";
import type {
  CanonicalRoomMessage,
  CreateMessageRequest,
  UpdateMessageRequest,
  CreateRoomRequest,
  MemberPrefsRequest,
  MessagePage,
  MemberRef,
  PatchRoomRequest,
  RoomMember,
  RoomSummary,
  RoomSection,
  UpsertRoomMemberRequest,
} from "./types";

export type SendState = "idle" | "sending" | "persisted" | "uncertain" | "rejected";

export interface OptimisticRoomMessage extends CreateMessageRequest {
  roomId: string;
  createdAt: string;
}

export interface TypingMember {
  userId: string;
  displayName: string;
  expiresAt: string;
}

export type { LiveRoomRun } from "./timeline";

/** The live-events context value both apps already build (web and mobile
 *  providers are structurally identical over these seven fields). */
export interface RoomsLiveSource {
  messaging: MessagingClient;
  onEvent(handler: (event: LiveEvent) => void): () => void;
  claimActiveRoomSubscription(room: { roomId: string; deliveryKey: string; workspaceId?: string | null }): () => void;
  liveChannels: readonly string[];
  connectionRevision: number;
  liveConnected: boolean;
  httpReady: boolean;
}

export interface RoomsPlatform {
  /** Signed-in user id, or null while identity is loading / signed out. */
  userId: string | null;
  /** Active organisation, or null for a personal workspace. */
  orgId: string | null;
  /**
   * The live-events context value. MUST be referentially stable across
   * renders, and so must everything on it: `messaging`, `onEvent`,
   * `claimActiveRoomSubscription`. Each is a dependency of an
   * effect inside the hook; a fresh object per render does not error, it
   * re-fires the history reconcile into "Maximum update depth exceeded".
   * Both apps' providers memoize their context value; a platform adapter
   * must pass it through, never rebuild it.
   */
  live: RoomsLiveSource;
  /** True when this room surface is on screen AND the app is foregrounded.
   *  Web: document visible + window focused. Mobile: AppState active. */
  isVisible(): boolean;
  /** Subscribe to changes in `isVisible()`. MUST be referentially stable, as must `isVisible`. */
  onVisibilityChange(listener: () => void): () => void;
  /** RFC-4122 v4. Web: crypto.randomUUID. Mobile: expo-crypto randomUUID. */
  uuid(): string;
}

export interface UseRoomsOptions {
  initialAgentId?: string | null;
  subscribeToRoom?: boolean;
  /** Select rooms[0] when nothing is active (the web sidebar's behaviour). */
  autoSelect?: boolean;
  /** Web's older-page control opts in; mobile keeps full history until its UI supports paging. */
  boundedHistory?: boolean;
}

export type UseRoomsResult = ReturnType<typeof useRooms>;

const NO_ROOMS: never[] = [];
const NO_SECTIONS: RoomSection[] = [];
const HISTORY_FAILED = "history could not be loaded";
const SELECT_FAILED = "this conversation could not be opened";

const failureMessage = (error: unknown, fallback: string): string =>
  error instanceof Error && error.message ? error.message : fallback;

export function useRooms(platform: RoomsPlatform, options: UseRoomsOptions = {}) {
  const { initialAgentId = null, subscribeToRoom = true, autoSelect = true, boundedHistory = false } = options;
  const { userId, orgId, live, isVisible, onVisibilityChange, uuid } = platform;
  const {
    messaging,
    claimActiveRoomSubscription,
    onEvent,
    connectionRevision,
    liveConnected,
    httpReady,
  } = live;
  const scope = userId ? `${userId}:${orgId ?? userId}` : null;
  const roomsResponse = useSWR(
    scope ? ["messaging-rooms", scope] : null,
    () => messaging.listRooms(),
    { revalidateOnFocus: false },
  );
  const roomsData = roomsResponse.data;
  const mutateRooms = roomsResponse.mutate;
  useEffect(() => {
    if (liveConnected) void mutateRooms();
  }, [connectionRevision, liveConnected, mutateRooms]);
  const rooms = roomsData?.rooms ?? NO_ROOMS;
  const sections = roomsData?.sections ?? NO_SECTIONS;
  /**
   * Members per room, for the sidebar.
   *
   * This used to be an SWR-backed fan-out: one `getRoom` per non-channel room,
   * all of which had to settle before the directory left its loading state, and
   * whose cache key included every room's `roomRevision` — so a rename or a
   * delivery-key rotation anywhere re-ran the whole fan-out and dropped the
   * sidebar back to "Loading conversations…". The room LIST now carries
   * `members`, so there is nothing to fetch and nothing to invalidate.
   *
   * Kept as a Map (rather than letting callers read `room.members`) only
   * because both apps already consume it in that shape.
   */
  const directoryRoomDetails: ReadonlyMap<string, RoomSummary> = useMemo(
    () => new Map(rooms.map((room) => [room.roomId, { ...room, members: room.members ?? [] }])),
    [rooms],
  );
  const [activeRoomId, setActiveRoomId] = useState<string | null>(null);
  const manuallyUnreadRoom = useRef<string | null>(null);
  const pendingRead = useRef<Promise<void> | null>(null);
  const dmRequests = useRef(new Map<string, string>());
  const handledInitialAgent = useRef<string | null>(null);
  const selectionRequest = useRef(0);
  const [accessRemoved, setAccessRemoved] = useState(false);
  // `selectAgentDm` is fired from an effect whose only caller is a `.catch`, so
  // a failed resolve-or-create used to vanish and leave the surface on a
  // spinner. Cleared by the next select or create that succeeds.
  const [selectRoomError, setSelectRoomError] = useState<string | null>(null);
  const [deliveryOverride, setDeliveryOverride] = useState<{ roomId: string; deliveryKey: string; roomRevision: number } | null>(null);
  const detailResponse = useSWR(
    scope && activeRoomId ? ["messaging-room", scope, activeRoomId] : null,
    () => messaging.getRoom(activeRoomId!),
    {
      revalidateOnFocus: false,
      refreshInterval: activeRoomId ? 60_000 : 0,
      shouldRetryOnError: false,
    },
  );
  const activeRoom = detailResponse.data;
  const roomError = detailResponse.error;
  const mutateRoom = detailResponse.mutate;
  useEffect(() => {
    if (manuallyUnreadRoom.current && manuallyUnreadRoom.current !== activeRoomId) {
      manuallyUnreadRoom.current = null;
    }
  }, [activeRoomId]);
  const [messages, setMessages] = useState<CanonicalRoomMessage[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyComplete, setHistoryComplete] = useState(false);
  // One state, two shapes on the surface: `historyError` stays a boolean for
  // the web timeline/search controls, `historyErrorMessage` carries the reason
  // so a client can name the failure instead of saying "something went wrong".
  const [historyErrorMessage, setHistoryErrorMessage] = useState<string | null>(null);
  const messagesRef = useRef(messages);
  messagesRef.current = messages;
  const [optimistic, setOptimistic] = useState<OptimisticRoomMessage[]>([]);
  const optimisticRef = useRef(optimistic);
  optimisticRef.current = optimistic;
  const [attempts, setAttempts] = useState(new Map<string, SendState>());
  const requests = useRef(new Map<string, CreateMessageRequest>());
  const [typing, setTyping] = useState(new Map<string, TypingMember>());
  const [runs, setRuns] = useState(new Map<string, LiveRoomRun>());
  const runsRef = useRef(runs);
  runsRef.current = runs;
  const generation = useRef(0);
  const requestedAfter = useRef<number | null>(null);
  const reconciliation = useRef<Promise<void> | null>(null);
  const historyAbort = useRef<AbortController | null>(null);
  const pageAbort = useRef<AbortController | null>(null);
  const initialRequest = useRef(0);
  const initialStarted = useRef<string | null>(null);
  const initialPending = useRef(false);
  const editDuringInitial = useRef(false);
  /**
   * Messages already fetched, per room.
   *
   * Room history is not SWR-backed, so before this cache every room switch
   * emptied the timeline and re-fetched from sequence 0 — including a switch
   * back to a room left seconds earlier. Re-opening a cached room now paints
   * its messages on the same frame as the click and reconciles only the delta
   * behind them.
   *
   * Cleared wholesale when `scope` changes: a different user or workspace must
   * never see another's messages, however briefly.
   */
  const historyCache = useRef(new Map<string, { messages: CanonicalRoomMessage[]; complete: boolean }>());
  const cacheRoom = useCallback((roomId: string, next: CanonicalRoomMessage[], complete?: boolean) => {
    const previous = historyCache.current.get(roomId);
    historyCache.current.set(roomId, { messages: next, complete: complete ?? previous?.complete ?? false });
  }, []);
  /** The ref, the rendered state and the cache always move together. */
  const applyMessages = useCallback((roomId: string, next: CanonicalRoomMessage[], complete?: boolean) => {
    messagesRef.current = next;
    setMessages(next);
    cacheRoom(roomId, next, complete);
  }, [cacheRoom]);

  useEffect(() => {
    historyCache.current.clear();
  }, [scope]);

  useEffect(() => {
    generation.current += 1;
    initialRequest.current += 1;
    initialStarted.current = null;
    initialPending.current = false;
    editDuringInitial.current = false;
    pageAbort.current?.abort();
    pageAbort.current = null;
    const cached = activeRoomId ? historyCache.current.get(activeRoomId) : undefined;
    messagesRef.current = cached?.messages ?? [];
    setMessages(cached?.messages ?? []);
    setHistoryLoading(false);
    setHistoryComplete(cached?.complete ?? false);
    setHistoryErrorMessage(null);
    setOptimistic([]);
    setAttempts(new Map());
    requests.current.clear();
    setTyping(new Map());
    setRuns(new Map());
    setAccessRemoved(false);
    setDeliveryOverride(null);
    requestedAfter.current = null;
    reconciliation.current = null;
    return () => {
      generation.current += 1;
      initialRequest.current += 1;
      initialPending.current = false;
      editDuringInitial.current = false;
      historyAbort.current?.abort();
      pageAbort.current?.abort();
      historyAbort.current = null;
      pageAbort.current = null;
    };
  }, [scope, activeRoomId]);

  const removeActiveAccess = useCallback(() => {
    // Fence the old read before aborting: its catch/finally must not restore
    // history state or cache after access has been revoked.
    generation.current += 1;
    historyAbort.current?.abort();
    pageAbort.current?.abort();
    historyAbort.current = null;
    pageAbort.current = null;
    reconciliation.current = null;
    requestedAfter.current = null;
    setHistoryLoading(false);
    setHistoryComplete(false);
    setHistoryErrorMessage(null);
    setAccessRemoved(true);
    if (activeRoomId) historyCache.current.delete(activeRoomId);
    messagesRef.current = [];
    setMessages([]);
    setOptimistic([]);
  }, [activeRoomId]);

  useEffect(() => {
    const error = roomError;
    if (!(error instanceof ApiError) || (error.status !== 403 && error.status !== 404)) return;
    removeActiveAccess();
  }, [roomError, removeActiveAccess]);

  const subscription = activeRoom && deliveryOverride?.roomId === activeRoom.roomId && deliveryOverride.roomRevision >= activeRoom.roomRevision
    ? { roomId: activeRoom.roomId, deliveryKey: deliveryOverride.deliveryKey, workspaceId: activeRoom.workspaceId ?? null }
    : activeRoom ? { roomId: activeRoom.roomId, deliveryKey: activeRoom.deliveryKey, workspaceId: activeRoom.workspaceId ?? null } : null;
  const subscriptionRoomId = subscription?.roomId ?? null;
  const subscriptionDeliveryKey = subscription?.deliveryKey ?? null;
  const subscriptionWorkspaceId = subscription?.workspaceId ?? null;

  useEffect(() => {
    if (!subscribeToRoom || accessRemoved || !subscriptionRoomId || !subscriptionDeliveryKey) return;
    return claimActiveRoomSubscription({
      roomId: subscriptionRoomId,
      deliveryKey: subscriptionDeliveryKey,
      workspaceId: subscriptionWorkspaceId,
    });
  }, [claimActiveRoomSubscription, subscriptionRoomId, subscriptionDeliveryKey, subscriptionWorkspaceId, accessRemoved, subscribeToRoom]);

  /**
   * Coalesced room-list refresh.
   *
   * Nearly every live event asked for the whole room list, one request per
   * event — so a burst of inbound messages became a burst of full list reads.
   * The list is not latency-critical (it backs unread counts and ordering), so
   * trailing-edge coalescing costs nothing a viewer can perceive.
   */
  const roomsRefresh = useRef<ReturnType<typeof setTimeout> | null>(null);
  const refreshRoomsSoon = useCallback(() => {
    if (roomsRefresh.current) return;
    roomsRefresh.current = setTimeout(() => {
      roomsRefresh.current = null;
      void mutateRooms();
    }, 400);
  }, [mutateRooms]);
  useEffect(() => () => {
    if (roomsRefresh.current) clearTimeout(roomsRefresh.current);
  }, []);

  /**
   * Clear any pending optimistic placeholder that a just-arrived canonical
   * message — from the live event OR a reconcile fetch, whichever wins the
   * race with this device's own `sendMessage` promise — has already made
   * redundant. Mirrors exactly what the success path in `sendMessage` does
   * (drop the optimistic row, mark the attempt `persisted`) so `sendState`
   * reporting stays consistent no matter which of the two settles first.
   *
   * The wire contract has no `clientMessageId` on a canonical message (only
   * the outgoing `CreateMessageRequest` carries one — see
   * `core/services/messaging.py`'s `canonical_message()` and the
   * messaging-projector's field allowlist, neither of which forward it), so
   * this can't do the id match the success path uses. It matches on (own
   * human author + identical text/reply/thread target) instead.
   *
   * ponytail: two identical pending sends from this user, to the same
   * reply/thread target, could both match the first confirmation. Add
   * `clientMessageId` to the canonical payload if that ever proves to matter
   * in practice.
   */
  const clearPersistedOptimistic = useCallback((incoming: readonly CanonicalRoomMessage[]) => {
    if (!userId) return;
    for (const message of incoming) {
      if (message.author.kind !== "human" || message.author.id !== userId) continue;
      const match = optimisticRef.current.find((row) =>
        row.text === message.text &&
        row.replyToMessageId === message.replyToMessageId &&
        row.threadRootMessageId === message.threadRootMessageId);
      if (!match) continue;
      setOptimistic((current) => current.filter((row) => row.clientMessageId !== match.clientMessageId));
      setAttempts((current) => new Map(current).set(match.clientMessageId, "persisted"));
    }
  }, [userId]);

  const reconcile = useCallback((afterSeq: number, force = false) => {
    if (!activeRoomId || accessRemoved) return Promise.resolve();
    // The newest-page read owns cold initialization. Its completion reconciles
    // from that page's high watermark, including events that arrived meanwhile.
    if (!force && (initialStarted.current !== activeRoomId || initialPending.current)) return Promise.resolve();
    requestedAfter.current = requestedAfter.current === null ? afterSeq : Math.min(requestedAfter.current, afterSeq);
    if (reconciliation.current) return reconciliation.current;
    const roomId = activeRoomId;
    const runGeneration = generation.current;
    const controller = new AbortController();
    historyAbort.current = controller;
    setHistoryLoading(true);
    setHistoryErrorMessage(null);
    const run = async () => {
      while (requestedAfter.current !== null && generation.current === runGeneration) {
        let cursor = requestedAfter.current;
        const completeHistory = cursor === 0;
        requestedAfter.current = null;
        for (;;) {
          const page: MessagePage = await messaging.listMessages(roomId, cursor, 100, { signal: controller.signal });
          if (generation.current !== runGeneration) return;
          const valid = page.messages.filter((message) => message.roomId === roomId);
          const merged = mergeCanonicalMessages(messagesRef.current, valid);
          // A reconcile fetch can deliver the very message a still-pending
          // `sendMessage` POST is waiting on (e.g. another live event's
          // sequence gap pulls in history while this device's own hung fetch
          // hasn't settled yet) -- clear its optimistic placeholder here too.
          clearPersistedOptimistic(valid);
          if (page.nextAfterSeq === null) {
            if (completeHistory) setHistoryComplete(true);
            applyMessages(roomId, merged, completeHistory ? true : undefined);
            break;
          }
          applyMessages(roomId, merged);
          cursor = page.nextAfterSeq;
        }
      }
    };
    reconciliation.current = run().catch((error) => {
      if (generation.current === runGeneration) {
        requestedAfter.current = null;
        setHistoryErrorMessage(failureMessage(error, HISTORY_FAILED));
        setHistoryComplete(false);
      }
      throw error;
    }).finally(() => {
      if (generation.current !== runGeneration || historyAbort.current !== controller) return;
      if (!pageAbort.current) setHistoryLoading(false);
      historyAbort.current = null;
      reconciliation.current = null;
      if (requestedAfter.current !== null) void reconcile(requestedAfter.current).catch(() => {});
    });
    return reconciliation.current;
  }, [activeRoomId, accessRemoved, messaging, applyMessages, clearPersistedOptimistic]);

  const loadOlder = useCallback(async () => {
    if (!activeRoomId || accessRemoved || historyLoading || historyComplete || !messagesRef.current.length) return;
    const roomId = activeRoomId;
    const runGeneration = generation.current;
    const controller = new AbortController();
    pageAbort.current = controller;
    setHistoryLoading(true);
    setHistoryErrorMessage(null);
    try {
      const page = await messaging.listMessages(roomId, 0, 100, { beforeSeq: messagesRef.current[0].messageSeq, signal: controller.signal });
      if (generation.current !== runGeneration || pageAbort.current !== controller) return;
      if (page.nextBeforeSeq === undefined) {
        pageAbort.current = null;
        void reconcile(0, true).catch(() => {});
        return;
      }
      applyMessages(roomId, mergeCanonicalMessages(messagesRef.current, page.messages.filter((message) => message.roomId === roomId)), page.nextBeforeSeq === null);
      setHistoryComplete(page.nextBeforeSeq === null);
    } catch (error) {
      if (generation.current === runGeneration && pageAbort.current === controller) setHistoryErrorMessage(failureMessage(error, HISTORY_FAILED));
    } finally {
      if (generation.current === runGeneration && pageAbort.current === controller) {
        pageAbort.current = null;
        if (!reconciliation.current) setHistoryLoading(false);
      }
    }
  }, [activeRoomId, accessRemoved, historyLoading, historyComplete, messaging, applyMessages, reconcile]);

  // `room.run.frame` (carrying the terminal `run_ended`) is EPHEMERAL over the
  // live channel, unlike `room.message.created` -- it has no `afterSeq` gap
  // recovery. A client that reconnects or rotates its delivery key mid-run can
  // miss that one frame and be stuck showing "Working" forever with a
  // Steer/Stop that no longer does anything useful. Ride the same
  // reconnect/rotation signal `reconcile` already uses to re-check any run
  // still marked "running" against the server's own truth.
  const reconcileRuns = useCallback(() => {
    const inFlight = [...runsRef.current.values()].filter((run) => run.state === "running");
    for (const run of inFlight) {
      void messaging.getRunStatus(run.runId).then((status) => {
        if (status.state !== "terminal") return;
        setRuns((current) => {
          const key = `${run.roomId}:${run.agentId}:${run.runId}`;
          const existing = current.get(key);
          if (!existing || existing.state !== "running") return current;
          return new Map(current).set(key, { ...existing, state: "completed" });
        });
      }).catch(() => {
        // Best-effort: a later reconnect (or a fresh frame proving the run is
        // still alive) gets another chance.
      });
    }
  }, [messaging]);

  /**
   * A run enters the map from `room.run.frame`, and the frame that declares one
   * (`run_active`) is published ONCE, at turn start. Only tabs subscribed at
   * that instant ever saw it: open, reload or re-select the room a minute later
   * and an agent that is still working shows nothing — no bubble, no Stop.
   *
   * The room detail carries the server's run ledger (`activeRuns`), so seed
   * from it. Frames merge on top of a seed unchanged (empty text/thinking/frames
   * is exactly what a run starts with), and a key already in the map is left
   * alone — including one this client watched END, since ended runs stay in the
   * map and a re-seed must never resurrect them.
   *
   * `reconcileRuns` then runs on every detail load. At the first seed the map is
   * still empty, so it costs nothing; on a later load it re-checks runs seeded
   * earlier against the ledger, which is what stops a seed that raced its run's
   * end from saying "Working" forever.
   */
  // Depending on the ARRAY, not on `activeRoom`: a read-mark rewrites the
  // detail in place (`mutateRoom(current => ({...current, unreadCount: 0}))`)
  // and would otherwise re-run this — and its reconcile — on every read.
  const activeRunSeeds = activeRoom?.roomId === activeRoomId ? activeRoom?.activeRuns : undefined;
  useEffect(() => {
    if (!activeRoomId || accessRemoved) return;
    if (activeRunSeeds?.length) {
      setRuns((current) => {
        let next: Map<string, LiveRoomRun> | null = null;
        for (const seed of activeRunSeeds) {
          const key = `${activeRoomId}:${seed.agentId}:${seed.runId}`;
          if (current.has(key)) continue;
          (next ??= new Map(current)).set(key, {
            roomId: activeRoomId,
            agentId: seed.agentId,
            runId: seed.runId,
            triggerMessageId: seed.triggerMessageId,
            threadRootMessageId: null,
            text: "",
            thinking: "",
            frames: [],
            state: "running",
          });
        }
        return next ?? current;
      });
    }
    reconcileRuns();
  }, [activeRunSeeds, activeRoomId, accessRemoved, reconcileRuns]);

  /**
   * Load history as soon as there IS a room — not when its live channel is
   * acknowledged.
   *
   * This used to wait for `liveChannels.includes(subscriptionPath)`, which put
   * two serial round trips in front of every room open: the room-detail GET
   * (needed only to learn the delivery key) and then a Clerk template-token
   * mint plus a subscribe acknowledgement. None of that is a precondition for
   * reading history — the subscription only ever delivered FUTURE messages, and
   * gaps are closed by `afterSeq` reconciliation, which is what the
   * `room.message.created` sequence check below already relies on.
   *
   * It was also a correctness problem: `liveChannels` is rebuilt as a fresh
   * array on every status emission (including acks for unrelated channels and
   * no-op channel diffs), so as an identity dependency it re-ran a FULL history
   * refetch of the open room on events that had nothing to do with it.
   *
   * A cached room asks only for what it is missing; a cold one starts at the
   * newest page so opening a long conversation does not mount its entire past.
   */
  useEffect(() => {
    if (!activeRoomId || accessRemoved) return;
    if (initialStarted.current === activeRoomId) return;
    const cached = historyCache.current.get(activeRoomId);
    if (cached?.messages.length) {
      initialStarted.current = activeRoomId;
      void reconcile(lastSequence(cached.messages)).catch(() => {});
      return;
    }
    if (!boundedHistory) {
      initialStarted.current = activeRoomId;
      void reconcile(0).catch(() => {});
      return;
    }
    const summary = rooms.find((room) => room.roomId === activeRoomId);
    const newest = summary?.lastMessageSeq ?? activeRoom?.lastMessageSeq;
    if (newest === undefined) return;
    initialStarted.current = activeRoomId;
    initialPending.current = true;
    const request = ++initialRequest.current;
    const runGeneration = generation.current;
    const controller = new AbortController();
    pageAbort.current = controller;
    setHistoryLoading(true);
    void messaging.listMessages(activeRoomId, 0, 100, { beforeSeq: newest + 1, signal: controller.signal }).then((page) => {
      if (generation.current !== runGeneration || initialRequest.current !== request) return;
      if (page.nextBeforeSeq === undefined) {
        pageAbort.current = null;
        initialPending.current = false;
        void reconcile(0).catch(() => {});
        return;
      }
      const valid = page.messages.filter((message) => message.roomId === activeRoomId);
      applyMessages(activeRoomId, mergeCanonicalMessages(valid, messagesRef.current), page.nextBeforeSeq === null);
      setHistoryComplete(page.nextBeforeSeq === null);
      pageAbort.current = null;
      initialPending.current = false;
      const after = editDuringInitial.current ? 0 : lastSequence(valid);
      editDuringInitial.current = false;
      void reconcile(after).catch(() => {});
    }).catch((error) => {
      if (generation.current === runGeneration && initialRequest.current === request) setHistoryErrorMessage(failureMessage(error, HISTORY_FAILED));
    }).finally(() => {
      if (generation.current === runGeneration && initialRequest.current === request) {
        pageAbort.current = null;
        initialPending.current = false;
        if (!reconciliation.current) setHistoryLoading(false);
      }
    });
    // `scope` is a dependency even though nothing here reads it: a signed-in
    // user or workspace change empties the cache above, and without re-running
    // this the timeline would stay empty until some unrelated event happened to
    // re-trigger it.
  }, [scope, activeRoomId, accessRemoved, boundedHistory, reconcile, rooms, activeRoom?.lastMessageSeq, messaging, applyMessages]);

  // Preserve socket/user-channel reconnect catch-up, including offline reads.
  useEffect(() => {
    if (!liveConnected || !activeRoomId || accessRemoved) return;
    void reconcile(lastSequence(messagesRef.current)).catch(() => {});
    reconcileRuns();
  }, [connectionRevision, liveConnected, activeRoomId, accessRemoved, reconcile, reconcileRuns]);

  // The user channel can be live before the room ACK. Close that blind spot
  // after each room/key ACK without depending on liveChannels array identity.
  const roomChannelSuffix = subscriptionRoomId && subscriptionDeliveryKey
    ? `/rooms/${subscriptionRoomId}/${subscriptionDeliveryKey}` : null;
  const acknowledgedRoomChannel = roomChannelSuffix
    ? live.liveChannels.find((channel) => subscriptionWorkspaceId
      ? channel === `/ws/${subscriptionWorkspaceId}${roomChannelSuffix}`
      : channel.endsWith(roomChannelSuffix)) ?? null
    : null;
  useEffect(() => {
    if (!liveConnected || !subscribeToRoom || !acknowledgedRoomChannel || accessRemoved) return;
    void reconcile(lastSequence(messagesRef.current)).catch(() => {});
    reconcileRuns();
  }, [liveConnected, subscribeToRoom, acknowledgedRoomChannel, accessRemoved, reconcile, reconcileRuns]);

  useEffect(() => onEvent((event) => {
    if (event.type === "room.message.updated" && event.payload.roomId === activeRoomId) {
      // ponytail: reread room history on edits; add targeted reads if long rooms make this costly.
      if (initialStarted.current !== activeRoomId || initialPending.current) editDuringInitial.current = true;
      else void reconcile(0).catch(() => {});
      return;
    }
    if (event.type === "room.message.available") {
      // The room jumps to the top of the list NOW, not after the coalesced
      // listRooms refetch (~400ms + a round trip) lands. Guarded by messageSeq
      // so a stale or replayed event never moves a room backwards or
      // double-counts unread; the trailing refresh reconciles with server
      // truth. Unread bumps only when the event says to notify AND the room
      // isn't the one on screen (markVisibleRead owns that case).
      const { roomId, messageSeq, notify } = event.payload;
      void mutateRooms((current) => {
        if (!current) return current;
        let changed = false;
        const rooms = current.rooms.map((room) => {
          if (room.roomId !== roomId || messageSeq <= room.lastMessageSeq) return room;
          changed = true;
          const bump = notify && !(roomId === activeRoomId && isVisible()) ? 1 : 0;
          return {
            ...room,
            lastMessageSeq: messageSeq,
            lastMessageAt: event.occurredAt,
            unreadCount: room.unreadCount + bump,
          };
        });
        return changed ? { ...current, rooms } : current;
      }, false);
      refreshRoomsSoon();
      return;
    }
    if (event.type === "room.access.removed") {
      // Removal or a channel delete: drop it from the list even when it isn't on screen.
      if (event.payload.roomId === activeRoomId) removeActiveAccess();
      void mutateRooms();
      return;
    }
    if (event.type === "room.subscription.rotated" && event.payload.roomId === activeRoomId) {
      setDeliveryOverride(event.payload);
      void mutateRoom();
      return;
    }
    if (event.type === "room.metadata.changed" && event.payload.roomId === activeRoomId) {
      void mutateRoom();
      void mutateRooms();
      return;
    }
    if (event.type === "room.member.updated") { refreshRoomsSoon(); return; }
    if (event.type === "room.message.reactions.changed" && event.payload.roomId === activeRoomId) {
      // Reactions must not extend the paged window and move its older cursor.
      if (boundedHistory && !messagesRef.current.some((message) => message.messageId === event.payload.messageId)) return;
      applyMessages(activeRoomId, mergeCanonicalMessages(messagesRef.current, [event.payload]));
      return;
    }
    if (event.type === "room.message.created" && event.payload.roomId === activeRoomId) {
      const previous = lastSequence(messagesRef.current);
      applyMessages(activeRoomId, mergeCanonicalMessages(messagesRef.current, [event.payload]));
      // Own send, confirmed by the live event before this device's own POST
      // promise settled -- clear the optimistic duplicate now rather than
      // waiting on that fetch (which may be hung; see sendMessage's timeout).
      clearPersistedOptimistic([event.payload]);
      if (event.payload.messageSeq > previous + 1) void reconcile(previous).catch(() => {});
      refreshRoomsSoon();
      return;
    }
    if (event.type === "room.typing.changed" && event.payload.roomId === activeRoomId) {
      setTyping((current) => {
        const next = new Map(current);
        if (Date.parse(event.payload.expiresAt) <= Date.now()) next.delete(event.payload.userId);
        else next.set(event.payload.userId, event.payload);
        return next;
      });
      return;
    }
    if (event.type === "room.run.frame" && event.payload.roomId === activeRoomId) {
      const payload = event.payload;
      const key = `${payload.roomId}:${payload.agentId}:${payload.runId}`;
      setRuns((current) => {
        const previous = current.get(key);
        const next = applyRunFrame(previous, payload);
        if (next === previous) return current;
        return new Map(current).set(key, next);
      });
    }
  }), [activeRoomId, boundedHistory, onEvent, mutateRoom, mutateRooms, reconcile, applyMessages, refreshRoomsSoon, clearPersistedOptimistic, removeActiveAccess, isVisible]);

  useEffect(() => {
    if (typing.size === 0) return;
    const timer = setInterval(() => {
      setTyping((current) => {
        const next = new Map([...current].filter(([, member]) => Date.parse(member.expiresAt) > Date.now()));
        return next.size === current.size ? current : next;
      });
    }, 1_000);
    return () => clearInterval(timer);
  }, [typing.size]);

  const markVisibleRead = useCallback(() => {
    const sequence = lastSequence(messages);
    if (!subscribeToRoom || !isVisible() ||
        !activeRoom || manuallyUnreadRoom.current === activeRoom.roomId ||
        sequence <= activeRoom.lastReadSeq || accessRemoved) return;
    // Marking read used to be followed by a full room-detail GET *and* a full
    // room-list GET, purely to learn an unread count we already know is zero.
    // Both are applied locally instead; the next natural revalidation confirms.
    const roomId = activeRoom.roomId;
    const request = messaging.markRead(roomId, { lastReadSeq: sequence }).then((cursor) => {
        // Apply the cursor the SERVER returned, not the one we asked for.
        //
        // It can come back LOWER than we sent: `set_read_cursor` clamps to the
        // room's own `lastSeq`, and another device marking the room unread
        // moves it backwards. In that case we are NOT caught up, so declaring
        // `unreadCount: 0` would be a lie — re-read instead and let the server
        // stay the source of truth. This used to re-read unconditionally; the
        // point of applying in place is to skip a room GET and a room-list GET
        // in the ordinary case, not to guess in the race.
        const applied = cursor.lastReadSeq;
        if (applied < sequence) {
          void mutateRoom();
          void mutateRooms();
          return;
        }
        void mutateRoom((current) => current && (current.roomId === roomId
          ? { ...current, lastReadSeq: applied, unreadCount: 0 } : current), false);
        void mutateRooms((current) => current && {
          ...current,
          rooms: current.rooms.map((room) => room.roomId === roomId
            ? { ...room, lastReadSeq: applied, unreadCount: 0 } : room),
        }, false);
      }).catch(() => {});
    const operation = pendingRead.current
      ? Promise.all([pendingRead.current, request]).then(() => {})
      : request;
    pendingRead.current = operation;
    void operation.finally(() => {
      if (pendingRead.current === operation) pendingRead.current = null;
    });
  }, [messages, activeRoom, accessRemoved, subscribeToRoom, isVisible, messaging, mutateRoom, mutateRooms]);

  // `markVisibleRead` closes over `messages`, so its identity changes on every
  // message. Reading it through a ref is what lets `selectRoom` be
  // referentially stable, which callers rely on to avoid re-running
  // room-selection effects on unrelated traffic. (The effect below still
  // re-subscribes as `messages` changes — the ref does not avoid that, and an
  // earlier version of this comment wrongly claimed it did.)
  const markVisibleReadRef = useRef(markVisibleRead);
  markVisibleReadRef.current = markVisibleRead;

  useEffect(() => {
    const run = () => markVisibleReadRef.current();
    run();
    return onVisibilityChange(run);
  }, [messages, activeRoom, onVisibilityChange]);

  const sendMessage = useCallback(async (request: CreateMessageRequest) => {
    if (!activeRoomId) throw new Error("No room selected.");
    const roomId = activeRoomId;
    const sendGeneration = generation.current;
    requests.current.set(request.clientMessageId, request);
    setAttempts((current) => new Map(current).set(request.clientMessageId, "sending"));
    setOptimistic((current) => current.some((message) => message.clientMessageId === request.clientMessageId)
      ? current
      : [...current, { ...request, roomId, createdAt: new Date().toISOString() }]);
    try {
      const canonical = await messaging.sendMessage(roomId, request);
      if (generation.current !== sendGeneration) {
        void mutateRooms();
        return canonical;
      }
      applyMessages(roomId, mergeCanonicalMessages(messagesRef.current, [canonical]));
      setOptimistic((current) => current.filter((message) => message.clientMessageId !== request.clientMessageId));
      setAttempts((current) => new Map(current).set(request.clientMessageId, "persisted"));
      void mutateRooms();
      return canonical;
    } catch (error) {
      if (generation.current === sendGeneration) {
        setOptimistic((current) => current.filter((message) => message.clientMessageId !== request.clientMessageId));
        // A live event (or a reconcile fetch) can already have confirmed this
        // exact send via `clearPersistedOptimistic` while this POST was still
        // hanging -- e.g. it's what eventually times out and lands here. Don't
        // let that late rejection downgrade an already-`persisted` attempt
        // back to `uncertain`.
        setAttempts((current) => current.get(request.clientMessageId) === "persisted"
          ? current
          : new Map(current).set(request.clientMessageId, error instanceof ApiError ? "rejected" : "uncertain"));
      }
      throw error;
    }
  }, [activeRoomId, messaging, mutateRooms]);

  const updateMessage = useCallback(async (messageId: string, request: UpdateMessageRequest) => {
    if (!activeRoomId || accessRemoved) throw new Error("No room selected.");
    const runGeneration = generation.current;
    try {
      const message = await messaging.updateMessage(activeRoomId, messageId, request);
      if (generation.current !== runGeneration || message.roomId !== activeRoomId) return;
      applyMessages(activeRoomId, mergeCanonicalMessages(messagesRef.current, [message]));
    } catch (error) {
      if (generation.current === runGeneration) void reconcile(0).catch(() => {});
      throw error;
    }
  }, [activeRoomId, accessRemoved, messaging, reconcile]);

  const setReaction = useCallback(async (messageId: string, emoji: string, active: boolean) => {
    if (!activeRoomId || accessRemoved) throw new Error("No room selected.");
    const runGeneration = generation.current;
    const message = await messaging.setReaction(activeRoomId, messageId, emoji, active);
    if (generation.current !== runGeneration || message.roomId !== activeRoomId) return;
    applyMessages(activeRoomId, mergeCanonicalMessages(messagesRef.current, [message]));
  }, [activeRoomId, accessRemoved, messaging]);

  const reconcileSend = useCallback(async (clientMessageId: string) => {
    const request = requests.current.get(clientMessageId);
    if (!request) return;
    await reconcile(lastSequence(messagesRef.current));
    return sendMessage(request);
  }, [reconcile, sendMessage]);
  const uploadAttachment = useCallback((image: RoomAttachmentInput) => {
    if (!activeRoomId) throw new Error("No room selected.");
    return messaging.uploadAttachment(activeRoomId, image);
  }, [activeRoomId, messaging]);
  const downloadAttachment = useCallback((roomId: string, attachmentId: string) => messaging.downloadAttachment(roomId, attachmentId), [messaging]);

  /** `null` releases the selection, which also stops the room-detail poll. */
  const selectRoom = useCallback((roomId: string | null) => {
    selectionRequest.current += 1;
    if (manuallyUnreadRoom.current === roomId) {
      manuallyUnreadRoom.current = null;
      markVisibleReadRef.current();
    }
    setActiveRoomId(roomId);
  }, []);

  const markUnread = useCallback(async (roomId: string, fromSeq?: number) => {
    if (activeRoomId === roomId) manuallyUnreadRoom.current = roomId;
    try {
      await pendingRead.current;
      await messaging.markUnread(roomId, fromSeq);
      await Promise.all([mutateRooms(), activeRoomId === roomId ? mutateRoom() : Promise.resolve()]);
    } catch (error) {
      if (manuallyUnreadRoom.current === roomId) manuallyUnreadRoom.current = null;
      throw error;
    }
  }, [activeRoomId, messaging, mutateRoom, mutateRooms]);

  /**
   * `activate: false` resolves or creates the room WITHOUT making it this
   * instance's active room. It exists for a picker (mobile's new-message sheet)
   * that hands the id to a navigation and then stays mounted: an instance left
   * holding a room it never displays keeps `detailResponse` polling it
   * (`refreshInterval: 60_000`) and reconciles its history for the rest of the
   * session, behind the screen that legitimately owns it.
   */
  const createRoom = useCallback(async (
    request: CreateRoomRequest,
    { requestId = ++selectionRequest.current, activate = true }: { requestId?: number; activate?: boolean } = {},
  ) => {
    const created = await messaging.createRoom(request);
    setSelectRoomError(null);
    await mutateRooms();
    if (activate && requestId === selectionRequest.current) setActiveRoomId(created.roomId);
    return created;
  }, [messaging, mutateRooms]);

  const resolveDm = useCallback(async (
    participant: MemberRef,
    { activate = true }: { activate?: boolean } = {},
  ): Promise<string | null> => {
    const requestId = ++selectionRequest.current;
    // A cold agent shortcut must await the list, not read an empty one as "no
    // DMs" and create a duplicate room. The list carries members, so this is
    // the only round trip between clicking a person and opening their DM.
    const listed = roomsData ?? await mutateRooms();
    if (!listed) throw new Error("Conversations are not ready.");
    if (requestId !== selectionRequest.current) return null;
    const existing = listed.rooms.find(
      // `members ?? []`: a client can still be holding a room list cached from
      // before the field existed on the summary.
      (room) => room.kind === "dm" && (room.members ?? []).some(
        (member) => member.kind === participant.kind && member.id === participant.id,
      ),
    );
    if (existing) {
      dmRequests.current.delete(`${participant.kind}:${participant.id}`);
      if (activate) setActiveRoomId(existing.roomId);
      return existing.roomId;
    }
    const key = `${participant.kind}:${participant.id}`;
    const clientRoomId = dmRequests.current.get(key) ?? uuid();
    dmRequests.current.set(key, clientRoomId);
    const created = await createRoom({
      clientRoomId,
      kind: "dm",
      name: null,
      members: [{ kind: participant.kind, id: participant.id, role: "member", listenMode: participant.kind === "agent" ? "active" : null }],
    }, { requestId, activate });
    dmRequests.current.delete(key);
    return requestId === selectionRequest.current ? created.roomId : null;
  }, [createRoom, roomsData, mutateRooms, uuid]);

  const selectDm = useCallback(async (
    participant: MemberRef,
    options: { activate?: boolean } = {},
  ): Promise<string | null> => {
    try {
      const roomId = await resolveDm(participant, options);
      setSelectRoomError(null);
      return roomId;
    } catch (error) {
      setSelectRoomError(failureMessage(error, SELECT_FAILED));
      throw error;
    }
  }, [resolveDm]);

  const selectAgentDm = useCallback(
    (agentId: string) => selectDm({ kind: "agent", id: agentId }),
    [selectDm],
  );

  useEffect(() => {
    if (!roomsData) return;
    if (!initialAgentId) {
      handledInitialAgent.current = null;
      // A child link effect may already have queued an explicit selection.
      if (autoSelect && !activeRoomId && rooms.length > 0) setActiveRoomId((current) => current ?? rooms[0].roomId);
      return;
    }
    const requestKey = `${scope}:${initialAgentId}`;
    if (handledInitialAgent.current === requestKey) return;
    handledInitialAgent.current = requestKey;
    const requestId = selectionRequest.current + 1;
    void selectAgentDm(initialAgentId).catch(() => {
      if (handledInitialAgent.current === requestKey && selectionRequest.current === requestId && autoSelect && rooms.length > 0) {
        setActiveRoomId(rooms[0].roomId);
      }
    });
  }, [activeRoomId, autoSelect, initialAgentId, rooms, roomsData, scope, selectAgentDm]);

  const updateRoom = useCallback(async (request: PatchRoomRequest) => {
    if (!activeRoomId) throw new Error("No room selected.");
    const updated = await messaging.updateRoom(activeRoomId, request);
    await Promise.all([mutateRoom(updated, false), mutateRooms()]);
    return updated;
  }, [activeRoomId, messaging, mutateRoom, mutateRooms]);

  const setRoomPrefs = useCallback(async (roomId: string, prefs: MemberPrefsRequest) => {
    // Optimistic so a star or a bell flips under the cursor, then reconciled.
    await mutateRooms(
      (current) => current && { ...current, rooms: current.rooms.map((room) => room.roomId === roomId ? { ...room, ...prefs, sectionId: prefs.sectionId === undefined ? room.sectionId : prefs.sectionId } : room) },
      false,
    );
    try {
      const updated = await messaging.setMemberPrefs(roomId, prefs);
      if (roomId === activeRoomId) await mutateRoom(updated, false);
    } finally {
      await mutateRooms();
    }
  }, [activeRoomId, messaging, mutateRoom, mutateRooms]);

  const saveSections = useCallback(async (next: readonly RoomSection[]) => {
    try {
      const response = await messaging.putSections({ sections: [...next] });
      // Merge like setRoomPrefs does, rather than replacing the cache entry
      // outright: an in-flight background revalidate (the live-event and
      // reconnect effects above call `mutateRooms()` with no debounce) can
      // resolve after this write and, with a wholesale replace, silently
      // clobber the section this call just saved back to the pre-save list.
      await mutateRooms((current) => current && { ...current, sections: response.sections, rooms: response.rooms }, false);
    } finally {
      await mutateRooms();
    }
  }, [messaging, mutateRooms]);

  const leaveRoom = useCallback(async (roomId: string) => {
    await messaging.leaveRoom(roomId);
    if (roomId === activeRoomId) setActiveRoomId(null);
    await mutateRooms();
  }, [activeRoomId, messaging, mutateRooms]);

  const deleteRoom = useCallback(async (roomId: string) => {
    await messaging.deleteRoom(roomId);
    if (roomId === activeRoomId) setActiveRoomId(null);
    await mutateRooms();
  }, [activeRoomId, messaging, mutateRooms]);

  const setPin = useCallback(async (messageId: string, pinned: boolean) => {
    if (!activeRoomId) throw new Error("No room selected.");
    const updated = await messaging.setPin(activeRoomId, messageId, pinned);
    await mutateRoom(updated, false);
  }, [activeRoomId, messaging, mutateRoom]);

  const upsertMember = useCallback(async (request: UpsertRoomMemberRequest) => {
    if (!activeRoomId) throw new Error("No room selected.");
    const updated = await messaging.upsertMember(activeRoomId, request);
    await mutateRoom(updated, false);
    return updated;
  }, [activeRoomId, messaging, mutateRoom]);

  const removeMember = useCallback(async (member: Pick<RoomMember, "kind" | "id">) => {
    if (!activeRoomId) throw new Error("No room selected.");
    await messaging.removeMember(activeRoomId, member.kind, member.id);
    await Promise.all([mutateRoom(), mutateRooms()]);
  }, [activeRoomId, messaging, mutateRoom, mutateRooms]);

  const typingSentAt = useRef(0);
  const setRoomTyping = useCallback((isTyping: boolean) => {
    if (!activeRoomId) return Promise.resolve();
    // The backend accepts one typing signal per second per room and answers
    // 429 otherwise; keystrokes arrive faster than that, so coalesce here and
    // treat a stray 429 as the no-op it is rather than an unhandled error.
    const now = Date.now();
    if (isTyping && now - typingSentAt.current < 1000) return Promise.resolve();
    typingSentAt.current = now;
    return messaging.setTyping({ roomId: activeRoomId, isTyping }).catch((error: unknown) => {
      if (error instanceof ApiError && error.status === 429) return;
      throw error;
    });
  }, [activeRoomId, messaging]);
  /**
   * Runs still worth showing as a live card.
   *
   * A run whose text was already persisted as a message (same runId, or — when
   * the persisted reply's runId doesn't match the run's — the same agent
   * posting identical trimmed text) is done double-duty; drop it so the
   * completed run card doesn't render again below the real message.
   *
   * Memoised: this is O(runs x messages) with a `trim()` per comparison, and it
   * was re-running on every render of the surface, including every keystroke in
   * the composer and every streamed chunk.
   */
  const messagesById = useMemo(() => new Map(messages.map(message => [message.messageId, message])), [messages]);
  const visibleRuns = useMemo(() => [...runs.values()].flatMap((run) => {
    const persisted = messages.some(message => message.runId === run.runId ||
      (run.text.trim() && message.author.kind === "agent" && message.author.id === run.agentId && message.text.trim() === run.text.trim()));
    if (persisted && run.state !== "running") return [];
    // Keep activity/controls after an interim reply without painting that reply twice.
    const visible = persisted ? { ...run, text: "" } : run;
    // Recover thread placement from history already loaded, without another HTTP/DB read.
    if (run.threadRootMessageId || !run.triggerMessageId || activeRoom?.kind === "dm") return [visible];
    const trigger = messagesById.get(run.triggerMessageId);
    return [{ ...visible, threadRootMessageId: trigger?.threadRootMessageId ?? run.triggerMessageId }];
  }), [runs, messages, messagesById, activeRoom?.kind]);
  const abortRun = useCallback((runId: string) => messaging.abortRun(runId), [messaging]);
  const steerRun = useCallback((runId: string, text: string) => messaging.steerRun(runId, { text }), [messaging]);
  const loadHistory = useCallback(() => {
    initialRequest.current += 1;
    pageAbort.current?.abort();
    pageAbort.current = null;
    initialPending.current = false;
    editDuringInitial.current = false;
    initialStarted.current = activeRoomId;
    void reconcile(0, true).catch(() => {});
  }, [activeRoomId, reconcile]);

  return {
    rooms,
    sections,
    roomsError: roomsResponse.error as Error | undefined,
    // `|| !scope` is load-bearing and matters MORE now, not less: the sidebar's
    // loading state used to be `roomsLoading || directoryLoading`, and with the
    // directory fan-out gone this is the only thing left standing between a
    // cold mount and a false "No conversations yet".
    roomsLoading: roomsResponse.isLoading || !scope,
    // Nothing is fetched for the directory any more — the room list is the
    // whole sidebar. Both are retained so consumers need not change at once.
    directoryError: undefined as Error | undefined,
    directoryLoading: false,
    activeRoomId,
    activeRoom,
    roomError: detailResponse.error as Error | undefined,
    roomLoading: detailResponse.isLoading,
    accessRemoved,
    selectRoomError,
    messages,
    historyLoading,
    historyComplete,
    historyError: historyErrorMessage !== null,
    historyErrorMessage,
    loadHistory,
    loadOlder,
    optimisticMessages: optimistic,
    typingMembers: [...typing.values()],
    runs: visibleRuns,
    liveConnected,
    httpReady,
    selectRoom,
    resourceSource: { client: messaging, onEvent, connectionRevision },
    selectDm,
    selectAgentDm,
    directoryRoomDetails,
    refreshRooms: () => mutateRooms(),
    refreshRoom: () => mutateRoom(),
    createRoom,
    updateRoom,
    setRoomPrefs,
    saveSections,
    leaveRoom,
    deleteRoom,
    setPin,
    upsertMember,
    removeMember,
    markUnread,
    sendMessage,
    setReaction,
    updateMessage,
    uploadAttachment,
    downloadAttachment,
    reconcileSend,
    sendState: (clientMessageId: string): SendState => attempts.get(clientMessageId) ?? "idle",
    setTyping: setRoomTyping,
    abortRun,
    steerRun,
  };
}
