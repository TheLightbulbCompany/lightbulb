"use client";

import { useEffect, useState } from "react";
import { useApi } from "@/lib/api";
import { useLiveEvents } from "@/hooks/useLiveEvents";

const RETRY_MS = 5000;

/** User ids on a live call in this room: seeded from the server, then kept by `room.call.changed`.
 *  Live events can drop, so the seed is re-read on every reconnect and retried when it fails. */
export function useRoomCallers(roomId: string | undefined): string[] {
  const api = useApi();
  const { onEvent, connectionRevision } = useLiveEvents();
  const [state, setState] = useState<{ roomId?: string; calls: Record<string, string> }>({ calls: {} }); // callId -> userId

  useEffect(() => {
    if (!roomId) return;
    let stale = false;
    let retry: ReturnType<typeof setTimeout> | undefined;
    const ended = new Set<string>(); // while a seed is in flight, the events newer than it
    const started: Record<string, string> = {};
    const own = (cur: typeof state) => (cur.roomId === roomId ? cur.calls : {});
    const off = onEvent((event) => {
      if (event.type !== "room.call.changed" || event.payload.roomId !== roomId) return;
      const { callId, userId, state: s } = event.payload;
      if (s === "ended") { ended.add(callId); delete started[callId]; } else { ended.delete(callId); started[callId] = userId; }
      setState((cur) => {
        const { [callId]: _gone, ...rest } = own(cur);
        return { roomId, calls: s === "started" ? { ...rest, [callId]: userId } : rest };
      });
    });
    const seed = () => {
      ended.clear();
      for (const k of Object.keys(started)) delete started[k];
      api.get(`/calls/active?roomId=${encodeURIComponent(roomId)}`)
        .then((raw) => {
          if (stale) return;
          const { calls } = raw as { calls: { callId: string; userId: string }[] };
          // Authoritative for what it saw (so a missed "ended" heals); events newer than it win.
          setState({ roomId, calls: { ...Object.fromEntries(calls.filter((c) => !ended.has(c.callId)).map((c) => [c.callId, c.userId])), ...started } });
        })
        .catch(() => { if (!stale) retry = setTimeout(seed, RETRY_MS); });
    };
    seed();
    return () => { stale = true; clearTimeout(retry); off(); };
  }, [api, onEvent, roomId, connectionRevision]);

  return state.roomId === roomId ? [...new Set(Object.values(state.calls))] : [];
}
