import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "../api/error";
import type { LiveEvent } from "../live/types";
import type { createMessagingClient } from "./client";
import type { LocalSessionEvent } from "./types";

export interface SessionSource {
  client: Pick<ReturnType<typeof createMessagingClient>, "listWorkspaceSessions" | "getLocalSession" | "listLocalSessionEvents" | "submitLocalSessionCommand" | "getLocalSessionCommand" | "setLocalSessionSharing">;
  onEvent: (handler: (event: LiveEvent) => void) => () => void;
  connectionRevision: number;
}

export const accessLost = (error: unknown) => error instanceof ApiError && (error.status === 403 || error.status === 404);

/**
 * A recoverable window over canonical history, never an unbounded transcript cache.
 *
 * `revision` (`useWorkspaceSessions`' `hintRevision`) is the primary tail signal, driven by
 * the relay's `workspace.sessions.changed` hint. `pollMs` is the backstop: a quiet
 * newer-page poll on a steady cadence so an open transcript keeps advancing
 * through throttled or dropped hints (the relay throttles transcript hints to
 * one per second per session with no trailing edge). Pass 0 to disable.
 * Intended for live output only: callers pass a nonzero cadence while the
 * session is `busy` and 0 otherwise, so idle sessions stay on hints plus the
 * 30s sessions safety poll instead of steady polling.
 */
export function useLocalSessionHistory(
  client: SessionSource["client"], sessionId: string,
  latestEventSeq: number, revision: number, onAccessLost: () => void,
  { pollMs = 0 }: { pollMs?: number } = {},
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
      const page = await client.listLocalSessionEvents(sessionId, after, limit);
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
  }, [client, sessionId, onAccessLost]);
  useEffect(() => {
    void load("latest");
    return () => { generation.current += 1; busy.current = false; };
  }, [load]);
  useEffect(() => {
    if (initialized.current && follow.current) void load("newer", { quiet: true });
  }, [revision, load]);
  useEffect(() => {
    if (!pollMs) return;
    const timer = setInterval(() => {
      if (initialized.current && follow.current) void load("newer", { quiet: true });
    }, pollMs);
    return () => clearInterval(timer);
  }, [pollMs, load]);
  return { events, loading, error, load, retry: () => load(lastDirection.current), hasOlder: (events[0]?.seq ?? 1) > 1,
    hasNewer: more || latestEventSeq > (events.at(-1)?.seq ?? 0) };
}
