"use client";

// What this room's requests made (a document, an app, a sent mail…), for the room's Files. The same first page the
// Requests column reads (one SWR key), so opening Files costs no extra request; older pages load on "Show older"
// and share the column's `:older` cache, so loading them in either place shows them in both.

import { useMemo } from "react";
import { useOlderTasks, useTaskList, type TaskScope, type TaskSummary } from "@lightbulb/shared";
import { useOptionalLiveEvents } from "@/hooks/useLiveEvents";

export type RoomOutput = { taskId: string; title: string; request: string };

export type RoomOutputs = { outputs: RoomOutput[]; hasOlder: boolean; loading: boolean; loadOlder: () => Promise<void> };

export function useRoomOutputs(roomId: string | null): RoomOutputs {
  const live = useOptionalLiveEvents();
  const client = live?.tasks as NonNullable<typeof live>["tasks"];
  // No live client (a room outside the app's providers): no list, so the client is never read.
  const scope = useMemo<TaskScope | null>(() => client && roomId ? { kind: "room", roomId } : null, [client, roomId]);
  const { data } = useTaskList(client, scope);
  const { older, hasOlder, loading, loadOlder } = useOlderTasks(client, data, scope);
  return { outputs: roomOutputs([...(data?.tasks ?? []), ...older]), hasOlder, loading, loadOlder };
}

export function roomOutputs(list: readonly TaskSummary[]): RoomOutput[] {
  return list.filter((t) => t.output).map((t) => ({ taskId: t.taskId, title: t.output!.title, request: t.title }));
}
