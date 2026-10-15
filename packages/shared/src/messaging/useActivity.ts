/**
 * The Activity feed (GET /messaging/activity), shared by web and mobile.
 *
 * Everything shown is server-rendered (`summary`, `actions`): this hook only
 * pages, patches and refreshes. Platform capabilities arrive through
 * `ActivityPlatform` — a structural subset of `RoomsPlatform`, so a client can
 * pass the one it already builds for `useRooms`. No DOM or react-native here.
 */
import { useCallback, useEffect, useMemo } from "react";
import { useSWRConfig } from "swr";
import useSWRInfinite from "swr/infinite";
import type { MessagingClient } from "./client";
import type { RoomsPlatform } from "./useRooms";
import { isNeedsYou } from "./activityView";
import type { ActivityActionInput, ActivityActionRequest, ActivityItem, ActivityPage } from "./types";

export type ActivityClient = Pick<MessagingClient, "listActivity" | "markActivitySeen" | "runActivityAction">;

export type ActivityPlatform = Pick<RoomsPlatform, "userId" | "orgId" | "isVisible" | "onVisibilityChange" | "uuid"> & {
  /** Only `onEvent` is read; MUST be referentially stable (both apps' providers are). */
  live: Pick<RoomsPlatform["live"], "onEvent">;
};

export interface UseActivityOptions {
  /** Page size (server default 30, max 100). */
  limit?: number;
  /** Coalesces a burst of live events / focus changes into one refetch. */
  debounceMs?: number;
}

const UNSEEN_CAP = 99;
const NO_PAGES: ActivityPage[] = [];

const time = (iso: string) => Date.parse(iso);

/** `list` with `item` swapped in by id; the same array when the id is absent. */
export function replaceActivityItem(list: ActivityItem[], item: ActivityItem): ActivityItem[] {
  const index = list.findIndex((entry) => entry.id === item.id);
  if (index < 0) return list;
  const next = list.slice();
  next[index] = item;
  return next;
}

/** The badge after `seenUpTo`: items created after it, capped at 99 like the server's. */
export function unseenActivityCount(items: readonly ActivityItem[], seenUpTo: string): number {
  const floor = time(seenUpTo);
  return Math.min(UNSEEN_CAP, items.filter((item) => time(item.createdAt) > floor).length);
}

/** What badges while unseen besides "needs you" (the server's rule): mentions, DMs and Paperclip items.
 *  A thread reply, a reaction or a finished session never does. */
const BADGES_WHILE_UNSEEN: readonly string[] = ["mention", "dm", "paperclip"];

/** The badge after `seenUpTo`: every "needs you" item plus unseen mentions, DMs and Paperclip items. */
export function attentionActivityCount(items: readonly ActivityItem[], seenUpTo: string): number {
  const pinned = items.filter(isNeedsYou);
  const rest = items.filter((item) => !isNeedsYou(item) && BADGES_WHILE_UNSEEN.includes(item.kind));
  return Math.min(UNSEEN_CAP, pinned.length + unseenActivityCount(rest, seenUpTo));
}

/** The newest `createdAt` (the `upTo` for mark-seen), or null for an empty feed. */
export function newestActivityAt(items: readonly ActivityItem[]): string | null {
  let newest: string | null = null;
  for (const item of items) if (newest === null || time(item.createdAt) > time(newest)) newest = item.createdAt;
  return newest;
}

/** Pages in order, first occurrence of an id wins. */
export function flattenActivityPages(pages: readonly ActivityPage[]): ActivityItem[] {
  const seen = new Set<string>();
  const items: ActivityItem[] = [];
  for (const page of pages) {
    for (const item of page.items) {
      if (seen.has(item.id)) continue;
      seen.add(item.id);
      items.push(item);
    }
  }
  return items;
}

/**
 * The request body for an answered input, generically: a text answer is a
 * note on a decision OR the text of a reply, and the server reads the one its
 * action needs; a select/person answer is the chosen value.
 */
export function activityActionRequest(input: ActivityActionInput | null, answer?: string): Pick<ActivityActionRequest, "note" | "value"> {
  if (!input || answer === undefined) return {};
  // A login / code answer is a secret: `value` only, never echoed back as a decision note.
  return input.kind === "text" ? { note: answer, value: answer } : { value: answer };
}

/** Per SWR cache and scope: one debounce timer and the last upTo sent, shared by every mounted copy (tab badge + screen). */
interface ActivityStore { timer: ReturnType<typeof setTimeout> | null; seenUpTo: string | null }
const stores = new WeakMap<object, Map<string, ActivityStore>>();
const storeFor = (cache: object, scope: string): ActivityStore => {
  let byScope = stores.get(cache);
  if (!byScope) stores.set(cache, byScope = new Map());
  let store = byScope.get(scope);
  if (!store) byScope.set(scope, store = { timer: null, seenUpTo: null });
  return store;
};

export function useActivity(client: ActivityClient, platform: ActivityPlatform, options: UseActivityOptions = {}) {
  const { limit = 30, debounceMs = 400 } = options;
  const { userId, orgId, live, isVisible, onVisibilityChange, uuid } = platform;
  const scope = userId ? `${userId}:${orgId ?? userId}` : null;
  const { cache } = useSWRConfig();
  const store = useMemo(() => (scope ? storeFor(cache, scope) : null), [cache, scope]);

  const response = useSWRInfinite<ActivityPage>(
    (index, previous: ActivityPage | null) => {
      if (!scope) return null;
      if (index === 0) return ["messaging-activity", scope, null];
      return previous?.next ? ["messaging-activity", scope, previous.next] : null;
    },
    ([, , cursor]: [string, string, string | null]) => client.listActivity({ cursor, limit }),
    { revalidateFirstPage: false, revalidateOnFocus: false, revalidateOnReconnect: false },
  );
  const { data, error, isLoading, isValidating, size, setSize, mutate } = response;
  const pages = data ?? NO_PAGES;
  const items = useMemo(() => flattenActivityPages(pages), [pages]);
  const hasMore = pages.length > 0 && pages[pages.length - 1].next !== null;
  const loadingMore = isValidating && size > pages.length;

  /** Refetches and resolves to the fresh items (a notification reads its approval's words from them). */
  const refresh = useCallback(async () => flattenActivityPages((await mutate()) ?? NO_PAGES), [mutate]);
  const scheduleRefresh = useCallback(() => {
    if (!store) return;
    if (store.timer) clearTimeout(store.timer);
    store.timer = setTimeout(() => { store.timer = null; void mutate(); }, debounceMs);
  }, [store, mutate, debounceMs]);

  // A notifying message (DM, mention, thread reply) may be a new item; the feed
  // refetches rather than patching from the event.
  // ponytail: Paperclip items refresh on focus/pull only — `paperclip.event` is mostly
  // heartbeat/log traffic and would refetch the feed (and Paperclip) during every run;
  // listen to it once it carries an approval-specific event name.
  // A session item is written with its own event; a pinned wait ends when the session moves on,
  // which only the workspace hint says (refetched only while such a wait is pinned).
  const waitingSession = items.some((item) => item.kind === "session" && isNeedsYou(item));
  useEffect(() => live.onEvent((event) => {
    if (event.type === "room.message.available" && event.payload.notify) scheduleRefresh();
    if (event.type === "workspace.session.attention") scheduleRefresh();
    // An agent asked for a website login or a code (website_logins.create_request).
    if (event.type === "runtime.resource.invalidated" && event.payload.resource === "activity") scheduleRefresh();
    if (event.type === "workspace.sessions.changed" && !event.payload.transcript && waitingSession) scheduleRefresh();
  }), [live, scheduleRefresh, waitingSession]);
  useEffect(() => onVisibilityChange(() => { if (isVisible()) scheduleRefresh(); }), [onVisibilityChange, isVisible, scheduleRefresh]);

  const loadMore = useCallback(() => {
    if (hasMore && !isValidating) void setSize((current) => current + 1);
  }, [hasMore, isValidating, setSize]);

  const markSeen = useCallback(async (upTo: string) => {
    if (!store || (store.seenUpTo && time(upTo) <= time(store.seenUpTo))) return;
    store.seenUpTo = upTo;
    try {
      await client.markActivitySeen(upTo);
    } catch (cause) {
      store.seenUpTo = null;
      throw cause;
    }
    await mutate((current) => current?.map((page, index) => {
      if (index !== 0) return page;
      const all = flattenActivityPages(current);
      return { ...page, unseenCount: unseenActivityCount(all, upTo), attentionCount: attentionActivityCount(all, upTo) };
    }), { revalidate: false });
  }, [client, store, mutate]);

  /** Rejects with the server's error (an ApiError) and leaves the item as it was. */
  const runAction = useCallback(async (itemId: string, actionId: string, request: Pick<ActivityActionRequest, "note" | "value"> = {}) => {
    const item = await client.runActivityAction(itemId, actionId, { ...request, idempotencyKey: uuid() });
    await mutate((current) => current?.map((page) => {
      const next = replaceActivityItem(page.items, item);
      return next === page.items ? page : { ...page, items: next };
    }), { revalidate: false });
    return item;
  }, [client, uuid, mutate]);

  return {
    items,
    unseenCount: pages[0]?.unseenCount ?? 0,
    /** The rail/tab badge: every needs-you item, plus unseen mentions, DMs and Paperclip items. */
    attentionCount: pages[0]?.attentionCount ?? pages[0]?.unseenCount ?? 0,
    paperclipAvailable: pages[0]?.sources.paperclip !== "unavailable",
    loading: isLoading,
    loadingMore,
    error: error as Error | undefined,
    hasMore,
    loadMore,
    refresh,
    markSeen,
    runAction,
  };
}

export type UseActivityResult = ReturnType<typeof useActivity>;
