"use client";

import useSWR from "swr";
import { useAuth } from "@clerk/nextjs";
import { useApi } from "@/lib/api";

interface RoomRow { roomId: string; kind: string; firstRun?: boolean; members?: { kind: string; id?: string }[] }

/** The member's DM with the assistant (the welcome DM first): Live there is owner mode, the Router by voice.
 *  undefined while loading, null when there is none. */
export function useAssistantDm(): RoomRow | null | undefined {
  const api = useApi();
  const { isLoaded, userId, orgId } = useAuth();
  const { data } = useSWR(isLoaded && userId && orgId ? ["/messaging/rooms#voice-dm", userId, orgId] : null, async () => {
    const { rooms } = (await api.get("/messaging/rooms")) as { rooms: RoomRow[] };
    return rooms.find((r) => r.firstRun) ?? rooms.find((r) => r.kind === "dm" && r.members?.some((m) => m.kind === "agent")) ?? null;
  });
  return data;
}
