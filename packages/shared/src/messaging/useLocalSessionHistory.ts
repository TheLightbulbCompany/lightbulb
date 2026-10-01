import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "../api/error";
import type { LiveEvent } from "../live/types";
import type { createMessagingClient } from "./client";
import { buildRows } from "./localSessionTranscript";
import type { LocalSessionEvent } from "./types";

export interface SessionSource {
  client: Pick<ReturnType<typeof createMessagingClient>, "listWorkspaceSessions" | "getLocalSession" | "listLocalSessionEvents" | "submitLocalSessionCommand" | "getLocalSessionCommand" | "setLocalSessionSharing"> &
    Partial<Pick<ReturnType<typeof createMessagingClient>, "cancelLocalSessionCommand">>;
  onEvent: (handler: (event: LiveEvent) => void) => () => void;
  connectionRevision: number;
}

/** Events a history window keeps. */
const RETAINED = 2000;
/** Older pages fetched on their own to fill a window that draws nothing. */
const EMPTY_FILL_PAGES = 10;

export const accessLost = (error: unknown) => error instanceof ApiError && (error.status === 403 || error.status === 404);

/**
 * A recoverable window over canonical history, never an unbounded transcript cache.
 *
 * The window FOLLOWS the tail: it opens on the newest page and keeps reading
 * forward (a full page means more already exists, so it reads the next one too)
 * until it has the end, then stays there as hints and polls arrive. Paging back
 * through older history does not stop that. `hasNewer` is true only when the
 * window no longer holds the tail -- the reader paged back past `RETAINED`
 * events and the newest were dropped -- and `load("latest")` returns to it.
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
  const inFlightDirection = useRef<"latest" | "older" | "newer">("latest");
  const tailOwed = useRef(false);
  // An error the reader caused (paging back, a retry) stays until they act on
  // it: the background reads go on underneath without clearing it, or the
  // retry would vanish at the next poll and `retry` would repeat the poll.
  const readerError = useRef(false);
  // What failed, so Retry repeats that and not whatever the reader did last.
  const failed = useRef<"latest" | "older" | "newer" | null>(null);
  const loadRef = useRef<(direction: "latest" | "older" | "newer", options?: { quiet?: boolean }) => Promise<void>>(undefined);
  const load = useCallback(async (direction: "latest" | "older" | "newer", { quiet = false }: { quiet?: boolean } = {}) => {
    const first = rows.current[0]?.seq ?? 1;
    const after = direction === "latest" ? Math.max(0, latest.current - 50)
      : direction === "older" ? Math.max(0, first - 51) : rows.current.at(-1)?.seq ?? 0;
    const limit = direction === "older" ? Math.min(50, first - 1) : 50;
    if (limit < 1) return;
    if (busy.current) {
      if (!inFlightQuiet.current || quiet) {
        // A tail read turned away is owed too, whatever is in flight: that
        // request may have asked before the output this one was told about,
        // and nothing else may ask again (the hint is spent, and an idle
        // session does not poll).
        if (quiet && direction !== "older") tailOwed.current = true;
        return;
      }
      generation.current += 1;
      // The read being superseded was fetching the tail; it is owed a second try.
      if (inFlightDirection.current !== "older") tailOwed.current = true;
    }
    const run = generation.current;
    busy.current = true;
    inFlightQuiet.current = quiet;
    inFlightDirection.current = direction;
    if (!quiet) {
      lastDirection.current = direction;
      readerError.current = false;
      setLoading(true);
      setError(null);
    }
    let readOn = false;
    let landed = false;
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
        // Scrollback keeps what the reader scrolled through, like a terminal's.
        // ponytail: RETAINED events kept; past it the far end is dropped and reloaded on demand.
        rows.current = direction === "older" ? ordered.slice(0, RETAINED) : ordered.slice(-RETAINED);
        setEvents(rows.current);
        // Paging back only stops the follow when it pushed the newest events out
        // of the window; until then the tail is still here and still advancing.
        if (direction === "older" && ordered.length > RETAINED) { follow.current = false; setMore(true); }
      }
      if (direction !== "older") {
        follow.current = true;
        setMore(false);
        // A full page: more exists past it already. Read on to the end now rather
        // than stopping a page short. An empty page never chains, so this ends.
        readOn = page.nextAfterSeq !== null && incoming.length > 0;
      }
      initialized.current = true;
      landed = true;
      // A background read that lands clears what a background read reported.
      if (!readerError.current) setError(null);
    } catch (e) {
      if (run !== generation.current) return;
      if (accessLost(e)) { onAccessLost(); return; }
      if (!quiet) readerError.current = true;
      // A background failure does not take over a retry the reader is owed.
      if (!quiet || !readerError.current) failed.current = direction;
      setError("Could not load session history. Try again.");
    } finally {
      if (run === generation.current) { busy.current = false; setLoading(false); }
    }
    // A load that failed leaves its error and its retry on screen; a quiet read
    // started now would clear them. What it owes waits for the retry.
    if (run !== generation.current || !landed) return;
    // Read on past a full page, and make good a tail read this load superseded:
    // nothing else is certain to ask for it a second time.
    const owed = tailOwed.current && follow.current;
    tailOwed.current = false;
    if (readOn || owed) void loadRef.current?.("newer", { quiet: true });
  }, [client, sessionId, onAccessLost]);
  loadRef.current = load;
  useEffect(() => {
    void load("latest");
    return () => { generation.current += 1; busy.current = false; };
  }, [load]);
  // Never an empty body: a page holding only rows the transcript does not draw (a
  // subagent's, the harness's) reaches back until something draws, a bounded number of pages.
  const fills = useRef(0);
  useEffect(() => {
    if (loading || error || !events.length || events[0].seq <= 1 || buildRows(events).length > 0) return;
    if (fills.current >= EMPTY_FILL_PAGES) return;
    fills.current += 1;
    void load("older");
  }, [events, loading, error, load]);
  useEffect(() => {
    if (initialized.current && follow.current) void load("newer", { quiet: true });
  }, [revision, load]);
  // The session row says events exist past the window: fetch them. It is a
  // reason to read, never something to show the reader.
  const lastSeq = events.at(-1)?.seq ?? 0;
  useEffect(() => {
    if (initialized.current && follow.current && latestEventSeq > lastSeq) void load("newer", { quiet: true });
  }, [latestEventSeq, lastSeq, load]);
  useEffect(() => {
    if (!pollMs) return;
    const timer = setInterval(() => {
      if (initialized.current && follow.current) void load("newer", { quiet: true });
    }, pollMs);
    return () => clearInterval(timer);
  }, [pollMs, load]);
  return { events, loading, error, load, retry: () => load(failed.current ?? lastDirection.current), hasOlder: (events[0]?.seq ?? 1) > 1,
    hasNewer: more };
}
