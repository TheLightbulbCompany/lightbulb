import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "../api/error";
import type { LiveEvent } from "../live/types";
import type { createMessagingClient } from "./client";
import type { LocalSessionEvent, RoomResources } from "./types";

export interface RoomResourceSource {
  client: Pick<ReturnType<typeof createMessagingClient>, "listResources" | "listWorkspaceSessions" | "listLocalSessionEvents" | "submitLocalSessionCommand" | "getLocalSessionCommand">;
  onEvent: (handler: (event: LiveEvent) => void) => () => void;
  connectionRevision: number;
}

export const accessLost = (error: unknown) => error instanceof ApiError && (error.status === 403 || error.status === 404);

/** Safety-net cadence for the resources poll. The push hint (`room.resources.changed`) is
 *  primary; this only covers a dropped hint. */
const RESOURCES_POLL_MS = 30_000;

/**
 * Mounted only by the open pane, keyed by owner, viewer, room and membership.
 * `enabled: false` neither fetches nor polls (a phone stacks several screens of
 * one room; only the focused one should); the last snapshot stays readable.
 */
export function useRoomResources(
  roomId: string,
  { client, onEvent, connectionRevision }: RoomResourceSource,
  { enabled = true, includeEnded = false }: { enabled?: boolean; includeEnded?: boolean } = {},
) {
  const [data, setData] = useState<RoomResources | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const [accessRevision, setAccessRevision] = useState(0);
  const request = useRef(0);
  const blocked = useRef(false);
  const refreshing = useRef(false);
  const queued = useRef(false);
  // Coalesced re-runs of the do-while below reuse the `refresh` closure that
  // was already in flight when a caller queued another pass; without this ref
  // that re-run would fetch with the `includeEnded` captured at the START of
  // the in-flight fetch, silently dropping a toggle flip that arrived mid-fetch.
  const includeEndedRef = useRef(includeEnded);
  includeEndedRef.current = includeEnded;
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
          const next = await client.listResources(roomId, { includeEnded: includeEndedRef.current });
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
  }, [client, roomId, revoke, includeEnded]);
  useEffect(() => {
    if (!enabled) return;
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
    const timer = setInterval(() => void refresh(), RESOURCES_POLL_MS);
    return () => { unsubscribe(); clearInterval(timer); blocked.current = true; invalidate(); };
  }, [enabled, onEvent, roomId, refresh, revoke, invalidate, includeEnded]);
  const previousConnection = useRef(connectionRevision);
  useEffect(() => {
    if (enabled && previousConnection.current !== connectionRevision) void refresh();
    previousConnection.current = connectionRevision;
  }, [enabled, connectionRevision, refresh]);
  return { data, error, loading, revision, accessRevision, refresh, revoke };
}

/** A recoverable window over canonical history, never an unbounded transcript cache. */
export function useLocalSessionHistory(
  client: RoomResourceSource["client"], roomId: string, sessionId: string,
  latestEventSeq: number, revision: number, onAccessLost: () => void,
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
  // Whether the in-flight load (if any) was quiet. A foreground load arriving
  // while a quiet background one is in flight supersedes it (bumping the epoch
  // so the stale quiet request's result/finally become no-ops below) instead of
  // being silently dropped — otherwise "Load older"/"Retry" clicks land on a
  // busy hook that no longer shows `loading` to disable them. A quiet load, or
  // a foreground load arriving while another foreground one is in flight, still
  // returns (in the foreground case the buttons are disabled anyway).
  const inFlightQuiet = useRef(false);
  const load = useCallback(async (direction: "latest" | "older" | "newer", { quiet = false }: { quiet?: boolean } = {}) => {
    const first = rows.current[0]?.seq ?? 1;
    const after = direction === "latest" ? Math.max(0, latest.current - 50)
      : direction === "older" ? Math.max(0, first - 51) : rows.current.at(-1)?.seq ?? 0;
    const limit = direction === "older" ? Math.min(50, first - 1) : 50;
    if (limit < 1) return;
    if (busy.current) {
      if (inFlightQuiet.current && !quiet) generation.current += 1;
      else return;
    }
    const run = generation.current;
    lastDirection.current = direction;
    busy.current = true;
    inFlightQuiet.current = quiet;
    if (!quiet) setLoading(true);
    setError(null);
    try {
      const page = await client.listLocalSessionEvents(roomId, sessionId, after, limit);
      if (run !== generation.current) return;
      const incoming = page.events.filter(event => event.seq > after && (direction !== "older" || event.seq < first));
      // A "newer"/"older" poll with nothing new keeps the existing array reference
      // so unchanged rows skip re-rendering.
      if (direction === "latest" || incoming.length > 0) {
        const merged = new Map((direction === "latest" ? [] : rows.current).map(event => [event.seq, event]));
        for (const event of incoming) merged.set(event.seq, event);
        const ordered = [...merged.values()].sort((a, b) => a.seq - b.seq);
        // ponytail: 200 events retained; navigation reloads discarded regions from canonical history.
        rows.current = direction === "older" ? ordered.slice(0, 200) : ordered.slice(-200);
        setEvents(rows.current);
      }
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
    if (initialized.current && follow.current) void load("newer", { quiet: true });
  }, [revision, load]);
  return { events, loading, error, load, retry: () => load(lastDirection.current), hasOlder: (events[0]?.seq ?? 1) > 1,
    hasNewer: more || latestEventSeq > (events.at(-1)?.seq ?? 0) };
}
