"use client";

import { useEffect, useState } from "react";
import { useApi } from "@/lib/api";
import { useLiveEvents } from "@/hooks/useLiveEvents";

const RETRY_MS = 5000;

/** User ids in this room's huddle: seeded from the server, then kept by `room.huddle.changed` (a full snapshot).
 *  Events can drop, so the seed is re-read on every reconnect; an event newer than the seed wins over it. */
export function useRoomHuddle(roomId: string | undefined): string[] {
  const api = useApi();
  const { onEvent, connectionRevision } = useLiveEvents();
  const [state, setState] = useState<{ roomId?: string; userIds: string[] }>({ userIds: [] });

  useEffect(() => {
    if (!roomId) return;
    let stale = false;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let eventSeen = false; // an event since the seed began is newer than the seed's answer
    const off = onEvent((event) => {
      if (event.type !== "room.huddle.changed" || event.payload.roomId !== roomId) return;
      eventSeen = true;
      setState({ roomId, userIds: event.payload.userIds });
    });
    const seed = () => {
      eventSeen = false;
      api.get(`/huddles/${encodeURIComponent(roomId)}`)
        .then((raw) => {
          if (stale || eventSeen) return;
          setState({ roomId, userIds: (raw as { userIds: string[] }).userIds });
        })
        .catch(() => { if (!stale) retry = setTimeout(seed, RETRY_MS); });
    };
    seed();
    return () => { stale = true; clearTimeout(retry); off(); };
  }, [api, onEvent, roomId, connectionRevision]);

  return state.roomId === roomId ? state.userIds : [];
}
