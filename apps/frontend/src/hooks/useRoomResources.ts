"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, type createMessagingClient, type LiveEvent, type LocalSessionEvent, type RoomResources } from "@lightbulb/shared";

export interface RoomResourceSource {
  client: Pick<ReturnType<typeof createMessagingClient>, "listResources" | "listLocalSessionEvents" | "submitLocalSessionCommand" | "getLocalSessionCommand">;
  onEvent: (handler: (event: LiveEvent) => void) => () => void;
  connectionRevision: number;
}

export const accessLost = (error: unknown) => error instanceof ApiError && (error.status === 403 || error.status === 404);

/** Mounted only by the open pane, keyed by owner, viewer, room and membership. */
export function useRoomResources(roomId: string, { client, onEvent, connectionRevision }: RoomResourceSource) {
  const [data, setData] = useState<RoomResources | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const [accessRevision, setAccessRevision] = useState(0);
  const request = useRef(0);
  const blocked = useRef(false);
  const refreshing = useRef(false);
  const queued = useRef(false);
  const invalidate = useCallback(() => {
    request.current += 1;
    refreshing.current = false;
    queued.current = false;
  }, []);
  const revoke = useCallback(() => {
    blocked.current = true;
    invalidate();
    setData(null);
    setLoading(false);
    setAccessRevision(v => v + 1);
    setError("Access to these resources was removed. Reopen the panel to check access.");
  }, [invalidate]);
  const refresh = useCallback(async () => {
    if (blocked.current) return;
    if (refreshing.current) { queued.current = true; return; }
    refreshing.current = true;
    const id = ++request.current;
    setLoading(true);
    try {
      // Hints coalesce; each successful snapshot applies before the queued refresh.
      do {
        queued.current = false;
        try {
          const next = await client.listResources(roomId);
          if (id !== request.current || blocked.current) return;
          setData(next);
          setError(null);
          setRevision(v => v + 1);
        } catch (e) {
          if (id !== request.current) return;
          if (accessLost(e)) { revoke(); return; }
          // Availability failure is not an authorization epoch change. Keep
          // the selected panel (and its immutable uncertain command) mounted.
          setError("Could not refresh devices and sessions. Try again.");
        }
      } while (queued.current && id === request.current && !blocked.current);
    } finally {
      if (id === request.current) { refreshing.current = false; setLoading(false); }
    }
  }, [client, roomId, revoke]);
  useEffect(() => {
    blocked.current = false;
    void refresh();
    const unsubscribe = onEvent(event => {
      if (!("roomId" in event.payload) || event.payload.roomId !== roomId) return;
      if (event.type === "room.access.removed") { revoke(); return; }
      if (event.type === "room.metadata.changed" || event.type === "room.subscription.rotated") {
        // Authorization changes start a new epoch immediately, without waiting
        // for an obsolete fetch; its completion cannot touch the new queue.
        invalidate();
        setData(null);
        setAccessRevision(v => v + 1);
        void refresh();
      } else if (event.type === "room.resources.changed") void refresh();
    });
    // Reuses the room's existing live subscription. Poll only while this pane exists.
    const timer = setInterval(() => void refresh(), 5000);
    return () => { unsubscribe(); clearInterval(timer); blocked.current = true; invalidate(); };
  }, [onEvent, roomId, refresh, revoke, invalidate]);
  const previousConnection = useRef(connectionRevision);
  useEffect(() => {
    if (previousConnection.current !== connectionRevision) void refresh();
    previousConnection.current = connectionRevision;
  }, [connectionRevision, refresh]);
  return { data, error, loading, revision, accessRevision, refresh, revoke };
}

/** A recoverable window over canonical history, never an unbounded transcript cache. */
export function useLocalSessionHistory(
  client: RoomResourceSource["client"], roomId: string, sessionId: string,
  latestEventSeq: number, revision: number, onAccessLost: () => void,
  live?: { onEvent: RoomResourceSource["onEvent"]; busy: boolean },
) {
  const [events, setEvents] = useState<LocalSessionEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [more, setMore] = useState(false);
  const rows = useRef(events);
  const latest = useRef(latestEventSeq);
  latest.current = latestEventSeq;
  const generation = useRef(0);
  const busy = useRef(false);
  const follow = useRef(true);
  const initialized = useRef(false);
  const lastDirection = useRef<"latest" | "older" | "newer">("latest");
  const load = useCallback(async (direction: "latest" | "older" | "newer") => {
    if (busy.current) return;
    const run = generation.current;
    const first = rows.current[0]?.seq ?? 1;
    const after = direction === "latest" ? Math.max(0, latest.current - 50)
      : direction === "older" ? Math.max(0, first - 51) : rows.current.at(-1)?.seq ?? 0;
    const limit = direction === "older" ? Math.min(50, first - 1) : 50;
    if (limit < 1) return;
    lastDirection.current = direction;
    busy.current = true;
    setLoading(true);
    setError(null);
    try {
      const page = await client.listLocalSessionEvents(roomId, sessionId, after, limit);
      if (run !== generation.current) return;
      const incoming = page.events.filter(event => event.seq > after && (direction !== "older" || event.seq < first));
      const merged = new Map((direction === "latest" ? [] : rows.current).map(event => [event.seq, event]));
      for (const event of incoming) merged.set(event.seq, event);
      const ordered = [...merged.values()].sort((a, b) => a.seq - b.seq);
      // ponytail: 200 events retained; navigation reloads discarded regions from canonical history.
      rows.current = direction === "older" ? ordered.slice(0, 200) : ordered.slice(-200);
      setEvents(rows.current);
      follow.current = direction !== "older" && page.nextAfterSeq === null;
      setMore(direction === "older" || page.nextAfterSeq !== null);
      initialized.current = true;
    } catch (e) {
      if (run !== generation.current) return;
      if (accessLost(e)) { onAccessLost(); return; }
      setError("Could not load session history. Try again.");
    } finally {
      if (run === generation.current) { busy.current = false; setLoading(false); }
    }
  }, [client, roomId, sessionId, onAccessLost]);
  useEffect(() => {
    void load("latest");
    return () => { generation.current += 1; busy.current = false; };
  }, [load]);
  useEffect(() => {
    if (initialized.current && follow.current) void load("newer");
  }, [revision, load]);
  const onEvent = live?.onEvent;
  const tailBusy = live?.busy ?? false;
  useEffect(() => {
    if (!onEvent) return;
    const unsubscribe = onEvent(event => {
      if (event.type === "room.resources.changed" && "roomId" in event.payload && event.payload.roomId === roomId && initialized.current && follow.current) void load("newer");
    });
    // Tail the session while a turn runs; the 5s resources poll covers idle sessions.
    const timer = setInterval(() => { if (initialized.current && follow.current) void load("newer"); }, tailBusy ? 1000 : 5000);
    return () => { unsubscribe(); clearInterval(timer); };
  }, [onEvent, tailBusy, roomId, load]);
  return { events, loading, error, load, retry: () => load(lastDirection.current), hasOlder: (events[0]?.seq ?? 1) > 1,
    hasNewer: more || latestEventSeq > (events.at(-1)?.seq ?? 0) };
}
