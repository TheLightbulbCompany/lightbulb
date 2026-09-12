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

/** Channel roomId → notifying messages seen live this session. */
export function useChannelMentions(): ReadonlyMap<string, number> {
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
  return mentions;
}
