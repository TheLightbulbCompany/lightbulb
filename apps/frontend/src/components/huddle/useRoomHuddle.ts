"use client";

import { useEffect, useState } from "react";
import { useApi } from "@/lib/api";
import { useLiveEvents } from "@/hooks/useLiveEvents";

const RETRY_MS = 30_000; // every open channel header reads this: back off hard while LiveKit is down

export interface RoomHuddle {
  /** Users in this room's huddle. */
  userIds: string[];
  /** Huddles are switched on (the server's LiveKit is configured). False until the seed answers. */
  available: boolean;
}

/** User ids in this room's huddle: seeded from the server, then kept by `room.huddle.changed` (a full snapshot).
 *  Events can drop, so the seed is re-read on every reconnect; an event newer than the seed wins over it. */
export function useRoomHuddle(roomId: string | undefined): RoomHuddle {
  const api = useApi();
  const { onEvent, connectionRevision } = useLiveEvents();
  const [state, setState] = useState<{ roomId?: string; userIds: string[] }>({ userIds: [] });
  const [available, setAvailable] = useState(false);

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
          if (stale) return;
          const { userIds, available: on } = raw as { userIds: string[]; available?: boolean };
          setAvailable(on === true);
          if (!eventSeen) setState({ roomId, userIds });
        })
        // A 404 is permanent (a shared-channel guest, a deleted room): stop asking.
        .catch((e: { status?: number }) => { if (!stale && e?.status !== 404) retry = setTimeout(seed, RETRY_MS); });
    };
    seed();
    return () => { stale = true; clearTimeout(retry); off(); };
  }, [api, onEvent, roomId, connectionRevision]);

  return { userIds: state.roomId === roomId ? state.userIds : [], available };
}
