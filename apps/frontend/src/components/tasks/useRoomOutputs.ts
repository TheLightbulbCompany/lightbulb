"use client";

// What this room's requests made (a document, an app, a sent mail…), for the room's Files. The same first page the
// Requests column reads (one SWR key), so opening Files costs no extra request. ponytail: first page only (50).

import { useTaskList, type TaskSummary } from "@lightbulb/shared";
import { useOptionalLiveEvents } from "@/hooks/useLiveEvents";

export type RoomOutput = { taskId: string; title: string; request: string };

export function useRoomOutputs(roomId: string | null): RoomOutput[] {
  const live = useOptionalLiveEvents();
  // No live client (a room outside the app's providers): no list, so the client is never read.
  const { data } = useTaskList(live?.tasks as NonNullable<typeof live>["tasks"], live?.tasks && roomId ? { kind: "room", roomId } : null);
  return roomOutputs(data?.tasks ?? []);
}

export function roomOutputs(list: readonly TaskSummary[]): RoomOutput[] {
  return list.filter((t) => t.output).map((t) => ({ taskId: t.taskId, title: t.output!.title, request: t.title }));
}
