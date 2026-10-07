import { useCallback, useEffect, useRef, useState } from "react";

import type { LocalSession } from "./types";
import type { SessionSource } from "./useLocalSessionHistory";

/**
 * Every live session in the workspace, for the sidebar, the glyph and the
 * mobile list. Polls slowly; the relay's `workspace.sessions.changed` hint on
 * the private user channel is what makes a new session show within a second.
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
  const refresh = useCallback(async () => {
    if (refreshing.current) { queued.current = true; return; }
    refreshing.current = true;
    const id = ++request.current;
    setLoading(true);
    try {
      do {
        queued.current = false;
        try {
          const next = await client.listWorkspaceSessions(includeEnded ? { includeEnded } : undefined);
          if (id !== request.current) return;
          setSessions(next.sessions);
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
      if (!event.payload.transcript) void refresh();
    });
    const timer = setInterval(() => void refresh(), 30000);
    return () => { unsubscribe(); clearInterval(timer); request.current += 1; refreshing.current = false; queued.current = false; };
  }, [enabled, onEvent, refresh]);
  const previousConnection = useRef(connectionRevision);
  useEffect(() => {
    if (enabled && previousConnection.current !== connectionRevision) void refresh();
    previousConnection.current = connectionRevision;
  }, [enabled, connectionRevision, refresh]);
  return { sessions, error, loading, refresh, hintRevision };
}
