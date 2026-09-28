import { applyRoomActivity, pruneRoomActivity, roomLiveLine, type RoomActivity, type RoomKind } from "@lightbulb/shared";
import { useCallback, useEffect, useState } from "react";

import { useLiveEvents } from "../providers/live-events";

const EMPTY: RoomActivity = new Map();

/**
 * Every room's live state from the private user channel — the Chats list's "typing…" /
 * "working…". Resets on `connectionRevision` (a reconnect — e.g. returning to the
 * foreground — may have dropped an "idle"): a fresh typing event re-adds within its ~1s
 * throttle, and a "working" hint comes back within ~30s, since the backend re-announces
 * "working" on every run heartbeat (Finding 6, preflight.md).
 */
export function useRoomActivity() {
  const { onEvent, connectionRevision } = useLiveEvents();
  const [activity, setActivity] = useState<RoomActivity>(EMPTY);
  useEffect(() => setActivity(EMPTY), [connectionRevision]);
  useEffect(
    () =>
      onEvent((event) => {
        if (event.type === "room.activity.changed") setActivity((current) => applyRoomActivity(current, event.payload));
      }),
    [onEvent],
  );
  // Ticks while any typing OR working entry is held, so an expired one (a dropped "idle", or a
  // crashed run's heartbeat going quiet) clears from the row even with no new event to redraw it.
  const hasActivity = activity.size > 0;
  useEffect(() => {
    if (!hasActivity) return;
    const timer = setInterval(() => setActivity((current) => pruneRoomActivity(current, Date.now())), 1_000);
    return () => clearInterval(timer);
  }, [hasActivity]);
  const liveLine = useCallback((roomId: string, kind: RoomKind) => roomLiveLine(activity, roomId, kind), [activity]);
  const working = useCallback(
    (roomId: string, agentId: string) => activity.get(roomId)?.get(`agent:${agentId}`)?.state === "working",
    [activity],
  );
  return { liveLine, working };
}
