"use client";

import { useEffect, useRef } from "react";
import useSWR from "swr";
import { useAuth } from "@clerk/nextjs";
import { useApi } from "@/lib/api";
import { useLiveEvents } from "@/hooks/useLiveEvents";

interface RoomRow { roomId: string; kind: string; firstRun?: boolean; members?: { kind: string; id?: string }[] }

/** While there is no DM yet, look again this often, this many times: a first open provisions the account, and the
 *  welcome DM is seeded a minute or so later (ensure_default_room). ponytail: a bounded poll, a room-created push
 *  would replace it. */
export const NO_DM_RETRY_MS = 5_000;
export const NO_DM_RETRIES = 60;

/** The member's DM with the assistant (the welcome DM first): Live there is owner mode, the Router by voice.
 *  undefined while loading, null when there is none yet. "None" is never final: it is looked up again on a live
 *  provisioning or membership event, and on a short bounded poll. */
export function useAssistantDm(): RoomRow | null | undefined {
  const api = useApi();
  const { isLoaded, userId } = useAuth();
  const { onEvent } = useLiveEvents();
  const misses = useRef(0);
  const { data, mutate } = useSWR(isLoaded && userId ? ["/messaging/rooms#voice-dm", userId] : null, async () => {
    const { rooms } = (await api.get("/messaging/rooms")) as { rooms: RoomRow[] };
    const dm = rooms.find((r) => r.firstRun) ?? rooms.find((r) => r.kind === "dm" && r.members?.some((m) => m.kind === "agent")) ?? null;
    misses.current = dm ? 0 : misses.current + 1;
    return dm;
  }, { refreshInterval: (latest) => latest === null && misses.current < NO_DM_RETRIES ? NO_DM_RETRY_MS : 0 });
  const missing = data === null;
  useEffect(() => {
    if (!missing) return;
    return onEvent((event) => {
      if (event.type === "runtime.resource.invalidated" || event.type === "room.member.updated") void mutate();
    });
  }, [missing, onEvent, mutate]);
  return data;
}
