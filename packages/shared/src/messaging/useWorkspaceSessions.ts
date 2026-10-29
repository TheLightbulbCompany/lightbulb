import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "../api/error";
import type { LocalSession } from "./types";
import type { SessionSource } from "./useLocalSessionHistory";

/**
 * Every live session in the workspace, for the sidebar, the glyph and the
 * mobile list. Polls slowly; the relay's `workspace.sessions.changed` hint on
 * the private user channel is what makes a new session show within a second.
 * A hint that names one session (`sessionId`) re-reads that row alone (one GET, ~0.3 s) and
 * patches it in: the whole list took 1.7-2.5 s (prod 2026-10-27), and every approval card,
 * Working/Waiting/Ended word and End chip waited for it. A row the list does not have yet, a
 * refused read, or a change of what the list admits (ended, sharing) still refetches the list,
 * in the background: the patched row is already on screen.
 */
export function useWorkspaceSessions(
  { client, onEvent, connectionRevision }: SessionSource,
  { enabled = true, includeEnded = false }: { enabled?: boolean; includeEnded?: boolean } = {},
) {
  const [sessions, setSessions] = useState<LocalSession[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [hintRevision, setHintRevision] = useState(0);
  const request = useRef(0);
  const refreshing = useRef(false);
  const queued = useRef(false);
  // A clock of reads: a row patched after a list read started is newer than that list's copy of it.
  const tick = useRef(0);
  const patched = useRef(new Map<string, { at: number; session: LocalSession }>());
  const refresh = useCallback(async () => {
    if (refreshing.current) { queued.current = true; return; }
    refreshing.current = true;
    const id = ++request.current;
    setLoading(true);
    try {
      do {
        queued.current = false;
        try {
          const startedAt = ++tick.current;
          const next = await client.listWorkspaceSessions(includeEnded ? { includeEnded } : undefined);
          if (id !== request.current) return;
          const newer = patched.current;
          setSessions(next.sessions.map(s => { const p = newer.get(s.sessionId); return p && p.at > startedAt ? p.session : s; }));
          for (const [sid, p] of newer) if (p.at <= startedAt) newer.delete(sid);
          setError(null);
        } catch {
          if (id !== request.current) return;
          setError("Could not refresh shared sessions.");
        }
      } while (queued.current && id === request.current);
    } finally {
      if (id === request.current) { refreshing.current = false; setLoading(false); }
    }
  }, [client, includeEnded]);
  const listed = useRef<LocalSession[]>([]);
  useEffect(() => { listed.current = sessions; }, [sessions]);
  // The latest read per session wins: an older one answering late is dropped.
  const reads = useRef(new Map<string, number>());
  const refreshOne = useCallback(async (sessionId: string) => {
    const was = listed.current.find(s => s.sessionId === sessionId);
    if (!was || !client.getLocalSession) { void refresh(); return; }
    const at = ++tick.current;
    reads.current.set(sessionId, at);
    let next: LocalSession;
    try { next = await client.getLocalSession(sessionId); }
    catch (e) { if (reads.current.get(sessionId) === at && e instanceof ApiError) void refresh(); return; }
    if (reads.current.get(sessionId) !== at) return;
    reads.current.delete(sessionId);
    patched.current.set(sessionId, { at, session: next });
    setSessions(list => list.map(s => s.sessionId === sessionId ? next : s));
    // What the list admits may have changed (an end, a pause, a fork): let it decide, off screen.
    if ((next.status === "ended") !== (was.status === "ended") || next.shared !== was.shared || next.forkedFrom !== was.forkedFrom) void refresh();
  }, [client, refresh]);
  const chatRooms = useRef(new Set<string>());
  useEffect(() => { chatRooms.current = new Set(sessions.flatMap((s) => s.chatRoomId ? [s.chatRoomId] : [])); }, [sessions]);
  useEffect(() => {
    if (!enabled) return;
    void refresh();
    // Every hint bumps hintRevision so an open session view pulls newer events; a transcript
    // batch only changes that content, not the list.
    const unsubscribe = onEvent(event => {
      // A top-level message in a listed session's chat: the list carries its unread count
      // (`chatUnread`), and the server's count is the only one (it leaves out the reader's own posts).
      if (event.type === "room.message.available" && chatRooms.current.has(event.payload.roomId)
        && !event.payload.threadRootMessageId && !event.payload.previewOnly) void refresh();
      if (event.type !== "workspace.sessions.changed") return;
      setHintRevision(v => v + 1);
      if (event.payload.transcript) return;
      if (event.payload.sessionId) void refreshOne(event.payload.sessionId);
      else void refresh();
    });
    const timer = setInterval(() => void refresh(), 30000);
    return () => { unsubscribe(); clearInterval(timer); request.current += 1; refreshing.current = false; queued.current = false; };
  }, [enabled, onEvent, refresh, refreshOne]);
  const previousConnection = useRef(connectionRevision);
  useEffect(() => {
    if (enabled && previousConnection.current !== connectionRevision) void refresh();
    previousConnection.current = connectionRevision;
  }, [enabled, connectionRevision, refresh]);
  /** Rows the server has just removed leave now, without waiting for the next refresh. */
  const forget = useCallback((gone: (session: LocalSession) => boolean) => setSessions(list => list.filter(s => !gone(s))), []);
  return { sessions, error, loading, refresh, hintRevision, forget };
}
