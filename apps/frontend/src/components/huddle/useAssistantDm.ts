"use client";

import useSWR from "swr";
import { useApi } from "@/lib/api";

interface RoomRow { roomId: string; kind: string; firstRun?: boolean; members?: { kind: string; id?: string }[] }

/** The member's DM with the assistant (the welcome DM first): Live there is owner mode, the Router by voice.
 *  undefined while loading, null when there is none. */
export function useAssistantDm(): RoomRow | null | undefined {
  const api = useApi();
  const { data } = useSWR("/messaging/rooms#voice-dm", async () => {
    const { rooms } = (await api.get("/messaging/rooms")) as { rooms: RoomRow[] };
    return rooms.find((r) => r.firstRun) ?? rooms.find((r) => r.kind === "dm" && r.members?.some((m) => m.kind === "agent")) ?? null;
  });
  return data;
}
