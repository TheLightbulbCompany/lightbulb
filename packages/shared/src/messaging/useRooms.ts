/**
 * The room state machine shared by the web and mobile clients: room list,
 * active-room detail, history reconciliation, optimistic sends, typing, live
 * runs, and read marking. Every platform-specific capability it needs —
 * identity, the live-events context value, visibility, uuid minting — arrives
 * through `RoomsPlatform`; there is no DOM global in here.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import { ApiError } from "../api/error";
import type { LiveEvent } from "../live/types";
import type { MessagingClient, RoomAttachmentInput } from "./client";
import { EMPTY_ROOM_SUBAGENTS, applySubagentEvent, delegationIdOf, liveSubagents, pruneFinishedSubagents, reconcileSubagentSnapshot, type RoomSubagents } from "./subagents";
import { applyRunFrame, declinedRun, lastSequence, mergeCanonicalMessages, runView, shownRunText, type LiveRoomRun } from "./timeline";
import type {
  ActiveRoomRun,
  CanonicalRoomMessage,
  CreateMessageRequest,
  UpdateMessageRequest,
  CreateRoomRequest,
  MemberPrefsRequest,
  MessagePage,
  MemberRef,
  PatchRoomRequest,
  RoomListResponse,
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
  /** Lightbulb's Router stage, the whole line ("Lightbulb is writing a reply…"), shown instead of "is typing…". */
  label?: string;
}

/** Read/delivery receipts for the active room. Seeded from its detail (`readCursors`,
 *  `agentDeliveries`, `activeRuns[].triggerMessageId`) and patched live by
 *  `room.read.changed` / `room.delivery.changed`. Reset when the active room changes. */
export interface RoomReceipts {
  /** userId -> lastReadSeq. A live human cursor takes the event's value outright. */
  readCursors: ReadonlyMap<string, number>;
  /** agentId -> lastDeliveredSeq. A live delivery keeps the max. */
  agentDeliveries: ReadonlyMap<string, number>;
  /** agentId -> messageIds whose run started ("read" for an agent — see
   *  `messaging.py::handle_run_event`). */
  agentReads: ReadonlyMap<string, ReadonlySet<string>>;
}
const NO_RECEIPTS: RoomReceipts = { readCursors: new Map(), agentDeliveries: new Map(), agentReads: new Map() };
/**
 * Merge a detail reseed onto the CURRENT receipts, never replace outright.
 * The detail's `activeRuns` holds only OPEN runs, so a plain replace would
 * wipe a live agent-read the instant its run ends (or on every same-room
 * revalidate: the 60s SWR poll, a `mutateRoom` after markRead, a
 * `room.metadata.changed` refetch) -- and a stale `agentDeliveries` row could
 * move a delivery cursor backward. A genuine room switch starts this from
 * `NO_RECEIPTS` (the reset effect above), so merging onto that is exactly a
 * fresh seed.
 */
const mergeReceiptSeeds = (
  current: RoomReceipts,
  readCursors: readonly { userId: string; lastReadSeq: number }[] | undefined,
  agentDeliveries: readonly { agentId: string; lastDeliveredSeq: number }[] | undefined,
  activeRuns: readonly ActiveRoomRun[] | undefined,
): RoomReceipts => {
  if (!readCursors?.length && !agentDeliveries?.length && !activeRuns?.length) return current;
  // A human cursor: the server is authoritative (mark-unread legitimately
  // lowers one) -- take the seed's value outright. A user the seed omits
  // (never live-patched, or an ex-member the backend already filtered out)
  // keeps whatever this client already had.
  const readCursorsNext = new Map(current.readCursors);
  for (const row of readCursors ?? []) readCursorsNext.set(row.userId, row.lastReadSeq);
  // A delivery cursor never moves backward.
  const agentDeliveriesNext = new Map(current.agentDeliveries);
  for (const row of agentDeliveries ?? []) {
    if (row.lastDeliveredSeq > (agentDeliveriesNext.get(row.agentId) ?? 0)) agentDeliveriesNext.set(row.agentId, row.lastDeliveredSeq);
  }
  // Agent reads: union. A run missing from this seed (ended, or never open)
  // must not drop a message this client already knows was read.
  const agentReadsNext = new Map([...current.agentReads].map(([agentId, set]) => [agentId, new Set(set)] as const));
  for (const run of activeRuns ?? []) {
    if (!run.triggerMessageId) continue;
    const set = agentReadsNext.get(run.agentId) ?? new Set<string>();
    set.add(run.triggerMessageId);
    agentReadsNext.set(run.agentId, set);
  }
  return { readCursors: readCursorsNext, agentDeliveries: agentDeliveriesNext, agentReads: agentReadsNext };
};

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
  /**
   * Keeps the room list and recent history across launches (mobile only; web
   * passes nothing). Read once per scope when that scope's store is created,
   * so a cold start paints before the network answers; written on every change
   * after. MUST be referentially stable.
   */
  persistence?: RoomsPersistence;
  /** The one screen showing the subagent strip opts in to the snapshot hydrate, the 5 s poll and the
   *  foreground refresh: each is a runtime invoke that extends its idle clock. Live events need none. */
  subagents?: boolean;
}

/** What `RoomsPersistence` keeps for one `userId:orgId` scope. */
export interface RoomsSnapshot {
  rooms: RoomListResponse;
  history: { roomId: string; messages: CanonicalRoomMessage[]; complete: boolean }[];
}
/** Bump when `RoomsSnapshot` or the types inside it change shape: a stored
 *  snapshot of another version is discarded, never read. */
export const ROOMS_SNAPSHOT_VERSION = 1;
export interface RoomsPersistence {
  /** The snapshot saved for exactly this scope, or null. */
  load(scope: string): RoomsSnapshot | null;
  save(scope: string, snapshot: RoomsSnapshot): void;
}

export type UseRoomsResult = ReturnType<typeof useRooms>;

const NO_ROOMS: never[] = [];
const NO_SECTIONS: RoomSection[] = [];
const HISTORY_FAILED = "history could not be loaded";

/** A DM is usable only when it has somebody besides the signed-in human. */
const hasDmRecipient = (room: RoomSummary, userId: string | null) =>
  room.kind !== "dm" || room.members.some((member) => member.kind !== "human" || member.id !== userId);
const SELECT_FAILED = "this conversation could not be opened";

const failureMessage = (error: unknown, fallback: string): string =>
  error instanceof Error && error.message ? error.message : fallback;

export interface MessageDelegations { agentId: string; delegationIds: string[] }

// An ended run object never changes, so its delegation ids are read once, not on every frame of another run.
const delegationIdsByRun = new WeakMap<LiveRoomRun, string[]>();
const runDelegationIds = (run: LiveRoomRun): string[] => {
  let ids = delegationIdsByRun.get(run);
  if (!ids) {
    ids = runView(run).tools.flatMap((t) => {
      const id = t.name === "delegate_task" ? delegationIdOf(t.result) : null;
      return id ? [id] : [];
    });
    delegationIdsByRun.set(run, ids);
  }
  return ids;
};

/** The saved message that carries this run's text: same runId, or the same agent with identical trimmed text. */
const isRunReply = (message: CanonicalRoomMessage, run: LiveRoomRun): boolean => !message.progress && (message.runId === run.runId ||
  (!!run.text.trim() && message.author.kind === "agent" && message.author.id === run.agentId && message.text.trim() === run.text.trim()));

/**
 * What every `useRooms` under one SWR cache shares, for ONE scope at a time.
 *
 * Mobile remounts the room screen per push and keeps three list copies
 * mounted, so per-instance state meant a refetch of the room's history on
 * every open and three room-list reads per event. Keyed off SWR's own cache so
 * the store lives exactly as long as (and as isolated as) the SWR data beside
 * it. A different scope — another user, another workspace, or signed out —
 * replaces the store outright: a lookup can never return another scope's
 * messages, however briefly.
 */
interface RoomsStore {
  scope: string | null;
  history: Map<string, { messages: CanonicalRoomMessage[]; complete: boolean }>;
  refresh: ReturnType<typeof setTimeout> | null;
  refreshedRevision: number | null;
  mounted: number;
  persistence?: RoomsPersistence;
  /** The list as last seen (network, optimistic patch or snapshot); what `persist` writes. */
  rooms: RoomListResponse | null;
  /** The snapshot's list: SWR fallback until the network answers. */
  fallback: RoomListResponse | undefined;
}
const stores = new WeakMap<object, RoomsStore>();
const storeFor = (cache: object, scope: string | null, persistence?: RoomsPersistence): RoomsStore => {
  let store = stores.get(cache);
  if (store?.scope !== scope) {
    const snapshot = scope ? persistence?.load(scope) ?? null : null;
    store = {
      scope, refresh: null, refreshedRevision: null, mounted: 0, persistence,
      history: new Map(snapshot?.history.map(({ roomId, messages, complete }) => [roomId, { messages, complete }])),
      rooms: snapshot?.rooms ?? null,
      fallback: snapshot?.rooms,
    };
    stores.set(cache, store);
  }
  return store;
};
// ponytail: 20 rooms per scope, least-recently-written evicted; a byte budget if long rooms make memory matter.
const HISTORY_ROOMS = 20;
// ponytail: the snapshot keeps the newest 50 messages of the 10 most recently written rooms; tune if cold opens still wait.
const PERSISTED_ROOMS = 10;
const PERSISTED_MESSAGES = 50;
/** Hand the store's current list + recent history to the platform (which debounces). */
const persist = (store: RoomsStore) => {
  if (!store.persistence || !store.scope || !store.rooms) return;
  const listed = new Set(store.rooms.rooms.map((room) => room.roomId));
  const history = [...store.history].filter(([roomId]) => listed.has(roomId)).slice(-PERSISTED_ROOMS)
    .map(([roomId, { messages, complete }]) => ({
      roomId,
      messages: messages.slice(-PERSISTED_MESSAGES),
      complete: complete && messages.length <= PERSISTED_MESSAGES,
    }));
  store.persistence.save(store.scope, { rooms: store.rooms, history });
};
/** A room this reader may have lost: out of memory and out of the snapshot. */
const forget = (store: RoomsStore, roomId: string) => {
  store.history.delete(roomId);
  if (store.rooms) store.rooms = { ...store.rooms, rooms: store.rooms.rooms.filter((room) => room.roomId !== roomId) };
  // Before the first network list the snapshot IS what renders; trim it too.
  if (store.fallback) store.fallback = { ...store.fallback, rooms: store.fallback.rooms.filter((room) => room.roomId !== roomId) };
  persist(store);
};
/** The newest sequence reachable from the first cached message with no hole. */
const contiguousSequence = (messages: readonly CanonicalRoomMessage[]) => {
  let end = messages[0]?.messageSeq ?? 0;
  for (const message of messages) {
    if (message.messageSeq > end + 1) break;
    end = Math.max(end, message.messageSeq);
  }
  return end;
};

/** The room list's SWR key. Exported so the web provider can start the read before the workspace mounts. */
export const roomsListKey = (userId: string, orgId: string | null | undefined) => ["messaging-rooms", `${userId}:${orgId ?? userId}`] as const;

export function useRooms(platform: RoomsPlatform, options: UseRoomsOptions = {}) {
  const { initialAgentId = null, subscribeToRoom = true, autoSelect = true, boundedHistory = false, persistence, subagents: watchSubagents = false } = options;
  const { userId, orgId, live, isVisible, onVisibilityChange, uuid } = platform;
  const {
    messaging,
    claimActiveRoomSubscription,
    onEvent,
    connectionRevision,
    liveConnected,
    httpReady,
  } = live;
  const roomsKey = userId ? roomsListKey(userId, orgId) : null;
  const scope = roomsKey?.[1] ?? null;
  const { cache } = useSWRConfig();
  const store = useMemo(() => storeFor(cache, scope, persistence), [cache, scope, persistence]);
  const roomsResponse = useSWR(
    roomsKey,
    () => messaging.listRooms(),
    { revalidateOnFocus: false, fallbackData: store.fallback },
  );
  const roomsData = roomsResponse.data;
  const mutateRooms = roomsResponse.mutate;
  const listing = roomsResponse.isValidating;
  // Once per reconnect across every mounted copy, not once per copy. After the list in flight lands, never over it:
  // SWR keeps only the newest request, so a cold load's first ack used to throw the first list away and every
  // project card waited for a second one (W14-4).
  useEffect(() => {
    if (!liveConnected) { store.refreshedRevision = null; return; }
    if (store.refreshedRevision === connectionRevision || listing) return;
    store.refreshedRevision = connectionRevision;
    void mutateRooms();
  }, [connectionRevision, liveConnected, listing, mutateRooms, store]);
  // The workspace feed is read on Activity, never as a conversation: it is in no list, no
  // unread count and no auto-pick. Recipient-less DMs are malformed and must not expose a
  // row or composer. An explicit feed select (Discuss) still opens it by id.
  const rooms = useMemo(
    () => roomsData?.rooms.filter((room) => !room.feed && hasDmRecipient(room, userId)) ?? NO_ROOMS,
    [roomsData, userId],
  );
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
    if (activeRoom && !hasDmRecipient(activeRoom, userId)) setActiveRoomId(null);
  }, [activeRoom, userId]);
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
  const [subagents, setSubagents] = useState<RoomSubagents>(EMPTY_ROOM_SUBAGENTS);
  const subagentsRef = useRef(subagents);
  subagentsRef.current = subagents;
  const activeRoomIdRef = useRef(activeRoomId);
  activeRoomIdRef.current = activeRoomId;
  const subagentPoll = useRef({ visible: false, failures: 0, inFlight: false });
  const [receipts, setReceipts] = useState<RoomReceipts>(NO_RECEIPTS);
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
   * Messages already fetched, per room, in the shared store (see
   * `RoomsStore`): re-opening a cached room — including on a freshly mounted
   * screen — paints its messages on the first frame and reconciles only the
   * delta behind them. A scope change swaps the store, so a different user or
   * workspace never sees another's messages.
   */
  const historyCache = store.history;
  const cacheRoom = useCallback((roomId: string, next: CanonicalRoomMessage[], complete?: boolean) => {
    // Another mounted copy (room screen + thread) may hold more of this room:
    // merge, never overwrite. Complete only while the merged set still starts
    // where a complete source started.
    const previous = historyCache.get(roomId);
    const messages = previous ? mergeCanonicalMessages(previous.messages, next) : next;
    const start = (list: readonly CanonicalRoomMessage[]) => list[0]?.messageSeq ?? Infinity;
    const isComplete = (complete === true && start(next) === start(messages)) ||
      (!!previous?.complete && start(previous.messages) === start(messages));
    historyCache.delete(roomId);
    historyCache.set(roomId, { messages, complete: isComplete });
    if (historyCache.size > HISTORY_ROOMS) historyCache.delete(historyCache.keys().next().value!);
    persist(store);
  }, [historyCache, store]);
  // Cached history paints only for a room the current list vouches for; a room
  // missing from it waits for the detail (and its 403/404) like a cold open.
  const roomsRef = useRef(rooms);
  roomsRef.current = rooms;
  const cachedHistory = useCallback((roomId: string) =>
    roomsRef.current.some((room) => room.roomId === roomId) ? historyCache.get(roomId) : undefined,
  [historyCache]);
  // A room the server stopped listing is one this reader may have lost; the
  // removal event can be dropped, so the list is the backstop.
  useEffect(() => {
    if (!roomsData) return;
    const listed = new Set(roomsData.rooms.map((room) => room.roomId));
    for (const roomId of [...historyCache.keys()]) if (!listed.has(roomId)) historyCache.delete(roomId);
    store.rooms = roomsData;
    persist(store);
  }, [roomsData, historyCache, store]);
  /** The ref, the rendered state and the cache always move together. */
  const applyMessages = useCallback((roomId: string, next: CanonicalRoomMessage[], complete?: boolean) => {
    messagesRef.current = next;
    setMessages(next);
    cacheRoom(roomId, next, complete);
  }, [cacheRoom]);

  useEffect(() => {
    generation.current += 1;
    initialRequest.current += 1;
    initialStarted.current = null;
    initialPending.current = false;
    editDuringInitial.current = false;
    pageAbort.current?.abort();
    pageAbort.current = null;
    const cached = activeRoomId ? cachedHistory(activeRoomId) : undefined;
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
    setSubagents(EMPTY_ROOM_SUBAGENTS);
    subagentPoll.current.failures = 0;
    setReceipts(NO_RECEIPTS);
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
    if (activeRoomId) forget(store, activeRoomId);
    messagesRef.current = [];
    setMessages([]);
    setOptimistic([]);
  }, [activeRoomId, store]);

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
  // One timer per store, shared by every mounted copy; the last copy to
  // unmount cancels it (the copy that armed it may leave while others stay).
  const refreshRoomsSoon = useCallback(() => {
    if (store.refresh) return;
    store.refresh = setTimeout(() => {
      store.refresh = null;
      void mutateRooms();
    }, 400);
  }, [mutateRooms, store]);
  useEffect(() => {
    store.mounted += 1;
    return () => {
      store.mounted -= 1;
      if (store.mounted === 0 && store.refresh) {
        clearTimeout(store.refresh);
        store.refresh = null;
      }
    };
  }, [store]);

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
            // `undefined` from a backend that predates the field: placed from the trigger below.
            threadRootMessageId: seed.threadRootMessageId,
            afterSeq: lastSequence(messagesRef.current),
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

  // Read/delivery receipts for the active room -- seeded from its detail. The
  // room-change reset above (`setReceipts(NO_RECEIPTS)`) is the only outright
  // replace; every reseed here (the 60s SWR poll, a `mutateRoom` after
  // markRead, a metadata refetch -- all of which change the detail's array
  // identity without a room switch) MERGES onto whatever this client already
  // learned live, per `mergeReceiptSeeds`.
  const readCursorSeeds = activeRoom?.roomId === activeRoomId ? activeRoom?.readCursors : undefined;
  const agentDeliverySeeds = activeRoom?.roomId === activeRoomId ? activeRoom?.agentDeliveries : undefined;
  useEffect(() => {
    if (!activeRoomId || accessRemoved) return;
    setReceipts((current) => mergeReceiptSeeds(current, readCursorSeeds, agentDeliverySeeds, activeRunSeeds));
  }, [activeRoomId, accessRemoved, readCursorSeeds, agentDeliverySeeds, activeRunSeeds]);

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
    const cached = cachedHistory(activeRoomId);
    if (cached?.messages.length && messagesRef.current.length) {
      initialStarted.current = activeRoomId;
      // From the last CONTIGUOUS sequence: a live event past a gap is cached
      // before its gap fill lands, and reconciling from the newest would skip
      // the hole for good.
      void reconcile(contiguousSequence(cached.messages)).catch(() => {});
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
  }, [scope, activeRoomId, accessRemoved, boundedHistory, reconcile, rooms, activeRoom?.lastMessageSeq, messaging, applyMessages, cachedHistory]);

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
  // Only one room channel is live at a time (another column, e.g. a request's progress, can hold it): without it
  // the room on screen catches up from the user channel's `room.message.available` instead.
  const roomChannelLive = useRef(false);
  roomChannelLive.current = liveConnected && subscribeToRoom && acknowledgedRoomChannel !== null;
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
      // truth. The counts mirror the server's (the list GET stays exact): unread
      // is a top-level message by someone else above my cursor, a mention is any
      // message naming me above it, and my own top-level post marks the room read.
      // The room on screen is left to markVisibleRead.
      // A thread reply never moves the room or its preview (the server's order ignores
      // replies too), and `previewOnly` -- an edit/delete of the current preview -- only
      // swaps the preview.
      const {
        roomId, messageSeq, threadRootMessageId, lastMessage, mentionedUserIds, accountableHumanId, previewOnly,
      } = event.payload;
      if (roomId === activeRoomId && !previewOnly && !roomChannelLive.current) {
        const newest = lastSequence(messagesRef.current);
        if (messageSeq > newest) void reconcile(newest).catch(() => {});
      }
      void mutateRooms((current) => {
        if (!current) return current;
        let changed = false;
        const rooms = current.rooms.map((room) => {
          if (room.roomId !== roomId) return room;
          if (previewOnly) {
            changed = true;
            return { ...room, lastMessage: lastMessage ?? null };
          }
          if (messageSeq <= room.lastMessageSeq) return room;
          changed = true;
          const top = !threadRootMessageId;
          const mine = accountableHumanId === userId
            || (lastMessage?.authorKind === "human" && lastMessage.authorId === userId);
          const counts = mine && top
            ? { lastReadSeq: messageSeq, unreadCount: 0, mentionCount: 0 }
            : (roomId === activeRoomId && isVisible()) || messageSeq <= room.lastReadSeq ? {} : {
              unreadCount: Math.min(99, room.unreadCount + (top && lastMessage ? 1 : 0)),
              mentionCount: Math.min(99, (room.mentionCount ?? 0)
                + (userId && mentionedUserIds?.includes(userId) ? 1 : 0)),
            };
          return {
            ...room,
            lastMessageSeq: messageSeq,
            ...(top ? {
              lastMessageAt: event.occurredAt,
              ...(lastMessage !== undefined ? { lastMessage } : {}),
            } : {}),
            ...counts,
          };
        });
        return changed ? { ...current, rooms } : current;
      }, false);
      refreshRoomsSoon();
      return;
    }
    if (event.type === "room.access.removed") {
      // Removal or a channel delete: drop it from the list even when it isn't on screen.
      const removedId = event.payload.roomId;
      forget(store, removedId);
      if (removedId === activeRoomId) removeActiveAccess();
      // Before the first network list the (just trimmed) snapshot is what is
      // on screen, so show it; either way the server list reconciles.
      void mutateRooms((current) => current ?? store.fallback, { revalidate: true });
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
      // The author's own line ends with its post (Lightbulb's stage line, before the backend's clear lands).
      const authorId = event.payload.author.id;
      setTyping((current) => current.has(authorId) ? new Map([...current].filter(([id]) => id !== authorId)) : current);
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
    if (event.type === "room.read.changed" && event.payload.roomId === activeRoomId) {
      const payload = event.payload;
      setReceipts((current) => {
        // The two `reader` shapes are distinguished by a top-level field
        // (`lastReadSeq` vs `messageId`), not just `reader.kind` -- narrow on
        // that so TS narrows the whole payload, not just the nested `reader`.
        if ("lastReadSeq" in payload) {
          const next = new Map(current.readCursors);
          next.set(payload.reader.id, payload.lastReadSeq);
          return { ...current, readCursors: next };
        }
        const existing = current.agentReads.get(payload.reader.id);
        if (existing?.has(payload.messageId)) return current;
        const nextSet = new Set(existing);
        nextSet.add(payload.messageId);
        const next = new Map(current.agentReads);
        next.set(payload.reader.id, nextSet);
        return { ...current, agentReads: next };
      });
      return;
    }
    if (event.type === "room.delivery.changed" && event.payload.roomId === activeRoomId) {
      const { agentId, lastDeliveredSeq } = event.payload;
      setReceipts((current) => {
        const existing = current.agentDeliveries.get(agentId) ?? 0;
        if (lastDeliveredSeq <= existing) return current;
        const next = new Map(current.agentDeliveries);
        next.set(agentId, lastDeliveredSeq);
        return { ...current, agentDeliveries: next };
      });
      return;
    }
    if (event.type === "room.subagent.event" && event.payload.roomId === activeRoomId) {
      const { agentId, events } = event.payload;
      const now = Date.now();
      setSubagents((s) => [...events].sort((a, b) => a.seq - b.seq)
        .reduce((acc, e) => applySubagentEvent(acc, agentId, e.type, e.payload, now), s));
      return;
    }
    if (event.type === "room.run.frame" && event.payload.roomId === activeRoomId) {
      const payload = event.payload;
      const key = `${payload.roomId}:${payload.agentId}:${payload.runId}`;
      // upstream's `message.start` boundary: a new turn clears that agent's finished subagents
      if (payload.frame.type === "run_active" && payload.frame.active) setSubagents((s) => pruneFinishedSubagents(s, payload.agentId));
      setRuns((current) => {
        const previous = current.get(key);
        const next = applyRunFrame(previous, payload);
        if (next === previous) return current;
        return new Map(current).set(key, previous ? next : { ...next, afterSeq: lastSequence(messagesRef.current) });
      });
    }
  }), [activeRoomId, boundedHistory, onEvent, mutateRoom, mutateRooms, reconcile, applyMessages, refreshRoomsSoon, clearPersistedOptimistic, removeActiveAccess, isVisible, store, userId]);

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
          ? { ...current, lastReadSeq: applied, unreadCount: 0, mentionCount: 0 } : current), false);
        void mutateRooms((current) => current && {
          ...current,
          rooms: current.rooms.map((room) => room.roomId === roomId
            ? { ...room, lastReadSeq: applied, unreadCount: 0, mentionCount: 0 } : room),
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
    const room = activeRoom ?? roomsData?.rooms.find((entry) => entry.roomId === activeRoomId);
    if (room && !hasDmRecipient(room, userId)) throw new Error("Direct message has no recipient.");
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
  }, [activeRoom, activeRoomId, messaging, mutateRooms, roomsData, userId]);

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
  const attachmentUrl = useCallback((roomId: string, attachmentId: string) => messaging.attachmentUrl(roomId, attachmentId), [messaging]);

  /** `null` releases the selection, which also stops the room-detail poll. */
  const selectRoom = useCallback((roomId: string | null) => {
    const listedRoom = roomId ? roomsData?.rooms.find((room) => room.roomId === roomId) : undefined;
    if (listedRoom && !hasDmRecipient(listedRoom, userId)) roomId = null;
    selectionRequest.current += 1;
    if (manuallyUnreadRoom.current === roomId) {
      manuallyUnreadRoom.current = null;
      markVisibleReadRef.current();
    }
    setActiveRoomId(roomId);
  }, [roomsData, userId]);

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

  // A room this person left or deleted leaves the list BEFORE the open room clears:
  // otherwise auto-select reopens it from the not-yet-refetched list, with a live
  // composer onto a room they no longer belong to.
  const dropRoom = useCallback(async (roomId: string) => {
    forget(store, roomId);
    await mutateRooms((current) => current && { ...current, rooms: current.rooms.filter((room) => room.roomId !== roomId) }, false);
    if (roomId === activeRoomId) setActiveRoomId(null);
    await mutateRooms();
  }, [activeRoomId, mutateRooms, store]);

  const leaveRoom = useCallback(async (roomId: string) => {
    await messaging.leaveRoom(roomId);
    await dropRoom(roomId);
  }, [dropRoom, messaging]);

  const deleteRoom = useCallback(async (roomId: string) => {
    await messaging.deleteRoom(roomId);
    await dropRoom(roomId);
  }, [dropRoom, messaging]);

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
   * completed run card doesn't render again below the real message. A progress
   * line is not the reply: neither its run id nor its text ever retires the card.
   *
   * Memoised: this is O(runs x messages) with a `trim()` per comparison, and it
   * was re-running on every render of the surface, including every keystroke in
   * the composer and every streamed chunk.
   */
  const messagesById = useMemo(() => new Map(messages.map(message => [message.messageId, message])), [messages]);
  const visibleRuns = useMemo(() => [...runs.values()].flatMap((run) => {
    const persisted = messages.some(message => isRunReply(message, run));
    if (persisted && run.state !== "running") return [];
    if (declinedRun(run)) return [];
    // Keep activity/controls after an interim reply without painting that reply twice.
    const trigger = run.triggerMessageId ? messagesById.get(run.triggerMessageId) : undefined;
    // Never anchored above its own trigger (a first frame can beat the trigger's live event).
    const anchored = trigger && (run.afterSeq ?? 0) < trigger.messageSeq ? { ...run, afterSeq: trigger.messageSeq } : run;
    const visible = persisted ? { ...anchored, text: "" } : { ...anchored, text: shownRunText(anchored.text) };
    // Recover thread placement from history already loaded, without another HTTP/DB read.
    if (run.threadRootMessageId !== undefined) return [visible];
    if (!run.triggerMessageId || activeRoom?.kind === "dm") return [{ ...visible, threadRootMessageId: null }];
    return [{ ...visible, threadRootMessageId: trigger?.threadRootMessageId ?? run.triggerMessageId }];
  }), [runs, messages, messagesById, activeRoom?.kind]);
  /**
   * The background subagents an ended run started, keyed by the saved reply that replaced its
   * card, so their DelegateCards stay under that message. Derived from the runs map, so it lives
   * as long as the strip does: until the room changes or the page reloads.
   *
   * Identity-stable by value, like RoomPanel's settledKey: every streamed frame replaces `runs`,
   * and a fresh Map here would re-render the whole history for a chunk of a live run.
   */
  const delegationsRef = useRef<{ key: string; map: ReadonlyMap<string, MessageDelegations> }>({ key: "", map: new Map() });
  const delegationsByMessage = useMemo(() => {
    const out = new Map<string, MessageDelegations>();
    for (const run of runs.values()) {
      if (run.state === "running") continue; // the live run card still shows its own
      const ids = runDelegationIds(run);
      const reply = ids.length ? [...messages].reverse().find((message) => isRunReply(message, run)) : undefined;
      if (!reply) continue;
      const entry = out.get(reply.messageId);
      out.set(reply.messageId, { agentId: run.agentId, delegationIds: [...(entry?.delegationIds ?? []), ...ids] });
    }
    const key = [...out].map(([id, entry]) => `${id}:${entry.agentId}:${entry.delegationIds.join(",")}`).join("|");
    if (key !== delegationsRef.current.key) delegationsRef.current = { key, map: out };
    return delegationsRef.current.map;
  }, [runs, messages]);
  const abortRun = useCallback((runId: string) => messaging.abortRun(runId), [messaging]);
  const refreshSubagents = useCallback(async () => {
    if (!activeRoomId) return;
    const roomId = activeRoomId;
    subagentPoll.current.inFlight = true;
    try {
      const { subagents: children } = await messaging.listSubagents(roomId);
      subagentPoll.current.failures = 0;
      if (roomId === activeRoomIdRef.current) setSubagents((s) => reconcileSubagentSnapshot(s, children, Date.now()));
    } catch {
      subagentPoll.current.failures += 1;
    } finally {
      subagentPoll.current.inFlight = false;
    }
  }, [activeRoomId, messaging]);

  useEffect(() => { if (watchSubagents) void refreshSubagents(); }, [watchSubagents, refreshSubagents]); // hydrate once per room

  // upstream's only polling: every 5 s while the strip is on screen and a row is live; 3 failures stop it until the page is visible again.
  useEffect(() => {
    if (!watchSubagents) return;
    const id = setInterval(() => {
      const p = subagentPoll.current;
      if (!p.visible || p.inFlight || p.failures >= 3 || !liveSubagents(subagentsRef.current).length) return;
      void refreshSubagents();
    }, 5_000);
    const unsubscribe = onVisibilityChange(() => {
      const p = subagentPoll.current;
      if (!p.visible || p.inFlight || !isVisible()) return;
      p.failures = 0;
      void refreshSubagents();
    });
    return () => { clearInterval(id); unsubscribe(); };
  }, [watchSubagents, refreshSubagents, onVisibilityChange, isVisible]);

  const subagentsPollVisible = useCallback((visible: boolean) => { subagentPoll.current.visible = visible; }, []);
  const steerSubagent = useCallback((agentId: string, subagentId: string, text: string) =>
    activeRoomId ? messaging.steerSubagent(activeRoomId, agentId, subagentId, text) : Promise.reject(new Error("No room selected.")), [activeRoomId, messaging]);
  const stopSubagent = useCallback((agentId: string, subagentId: string) =>
    activeRoomId ? messaging.stopSubagent(activeRoomId, agentId, subagentId) : Promise.reject(new Error("No room selected.")), [activeRoomId, messaging]);

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
    // `&& !roomsData`: a persisted list is not loading, even while SWR revalidates it.
    roomsLoading: (roomsResponse.isLoading && !roomsData) || !scope,
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
    // ponytail: agent reads are live-only; persist a READ#agent# row if this matters.
    receipts,
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
    attachmentUrl,
    reconcileSend,
    sendState: (clientMessageId: string): SendState => attempts.get(clientMessageId) ?? "idle",
    setTyping: setRoomTyping,
    abortRun,
    steerRun,
    subagents,
    delegationsByMessage,
    refreshSubagents,
    steerSubagent,
    stopSubagent,
    subagentsPollVisible,
  };
}
