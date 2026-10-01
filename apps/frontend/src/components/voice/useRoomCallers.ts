"use client";

import { useEffect, useState } from "react";
import { useApi } from "@/lib/api";
import { useLiveEvents } from "@/hooks/useLiveEvents";

/** User ids on a live call in this room: seeded from the server, then kept by `room.call.changed`. */
export function useRoomCallers(roomId: string | undefined): string[] {
  const api = useApi();
  const { onEvent } = useLiveEvents();
  const [state, setState] = useState<{ roomId?: string; ids: string[] }>({ ids: [] });

  useEffect(() => {
    if (!roomId) return;
    let stale = false;
    const off = onEvent((event) => {
      if (event.type !== "room.call.changed" || event.payload.roomId !== roomId) return;
      const { userId, state: s } = event.payload;
      setState((cur) => {
        const ids = (cur.roomId === roomId ? cur.ids : []).filter((id) => id !== userId);
        return { roomId, ids: s === "started" ? [...ids, userId] : ids };
      });
    });
    api.get(`/calls/active?roomId=${encodeURIComponent(roomId)}`)
      .then((raw) => { const r = raw as { calls: { userId: string }[] }; if (!stale) setState((cur) => ({ roomId, ids: [...new Set([...r.calls.map((c) => c.userId), ...(cur.roomId === roomId ? cur.ids : [])])] })); })
      .catch(() => {}); // the marker is decoration; live events still fill it in
    return () => { stale = true; off(); };
  }, [api, onEvent, roomId]);

  return state.roomId === roomId ? state.ids : [];
}
