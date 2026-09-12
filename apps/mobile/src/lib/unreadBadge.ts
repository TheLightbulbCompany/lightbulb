import type { LiveEvent, RoomSummary } from "@lightbulb/shared";
import { useEffect, useState } from "react";

import { useLiveEvents } from "../providers/live-events";

/**
 * Slack's rule: the badge counts DMs and mentions, never plain channel
 * unreads, and never a muted conversation of either kind. The projector
 * already makes the mention call — `room.message.available` carries
 * `notify: true` for exactly "DM, mention, or your thread" — so the client
 * mirrors it rather than re-deriving it.
 *
 * Channel mentions are capped at that channel's unreadCount, which means a
 * server-side read clears them for free and no manual reset is needed.
 *
 * ponytail: mentions are counted only for this app session; a cold start
 * shows DM unreads until the first list refresh. Add a persisted per-room
 * mention counter server-side if that gap is ever noticed.
 */
export function badgeCount(
  rooms: readonly RoomSummary[],
  channelMentions: ReadonlyMap<string, number>,
): number {
  return rooms.reduce((sum, room) => {
    if (room.muted) return sum;
    return (
      sum +
      (room.kind === "channel"
        ? Math.min(channelMentions.get(room.roomId) ?? 0, room.unreadCount)
        : room.unreadCount)
    );
  }, 0);
}

/**
 * roomId → notifying messages seen live this session.
 *
 * Every room's notifying messages are counted, not just a channel's — the event
 * carries no kind and the rooms list is the only place that knows one. DM
 * entries are inert: `badgeCount` reads this map for channels only, since a
 * DM's own `unreadCount` already counts every message in it.
 *
 * `rooms` is how a count ends: once the server reports a room read
 * (`unreadCount === 0`) its entry is DROPPED. The cap in `badgeCount` is not
 * enough on its own — it hides a read mention only while the room stays read,
 * and resurrects it the moment a plain message lifts `unreadCount` again.
 */
export function useChannelMentions(
  rooms: readonly RoomSummary[],
): ReadonlyMap<string, number> {
  const { onEvent } = useLiveEvents();
  const [mentions, setMentions] = useState<ReadonlyMap<string, number>>(new Map());
  useEffect(
    () =>
      onEvent((event: LiveEvent) => {
        if (event.type !== "room.message.available" || !event.payload.notify) return;
        setMentions((current) =>
          new Map(current).set(
            event.payload.roomId,
            (current.get(event.payload.roomId) ?? 0) + 1,
          ),
        );
      }),
    [onEvent],
  );
  // `rooms` gets a fresh array identity on every list refresh, so this runs
  // often and returns `current` unchanged when there is nothing to drop —
  // React bails out on identical state, so no re-render.
  useEffect(() => {
    setMentions((current) => {
      let next: Map<string, number> | null = null;
      for (const room of rooms) {
        if (room.unreadCount === 0 && current.has(room.roomId)) {
          next ??= new Map(current);
          next.delete(room.roomId);
        }
      }
      return next ?? current;
    });
  }, [rooms]);
  return mentions;
}
