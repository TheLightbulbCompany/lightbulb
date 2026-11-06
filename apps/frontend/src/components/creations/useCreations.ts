"use client";

// What tasks made (`GET /tasks/outputs`), for Creations, a project's Made here strip and its Files view. A page is
// read until it holds 12 outputs (or as many as `limit` asks) or the server has no more: `limit` counts tasks, not
// outputs. "Load more" reads the next page from `next`.

import { useEffect, useMemo, useRef } from "react";
import useSWRInfinite from "swr/infinite";
import { useTaskList, type CreationOutput, type OutputsFilter } from "@lightbulb/shared";
import { useLiveEvents } from "@/hooks/useLiveEvents";

export type CreationsFilter = Omit<OutputsFilter, "cursor">;
type Page = { outputs: CreationOutput[]; next: string | null };

const PAGE = 12;

/** `null` filter = no request (the switch is off). */
export function useCreations(filter: CreationsFilter | null) {
  const { tasks } = useLiveEvents();
  const base = filter ? JSON.stringify(filter) : null;
  const want = Math.min(PAGE, filter?.limit ?? PAGE);
  const { data, error, size, setSize, isLoading, isValidating, mutate } = useSWRInfinite<Page>(
    (index, prev: Page | null) => (!base || (prev && !prev.next) ? null : ["creations", tasks.cacheScope, base, index === 0 ? null : prev?.next ?? null]),
    async ([, , , cursor]: [string, string, string, string | null]) => {
      let page: Page = { outputs: [], next: cursor };
      do {
        const read = await tasks.outputs({ ...filter, cursor: page.next });
        page = { outputs: [...page.outputs, ...read.outputs], next: read.next };
      } while (page.outputs.length < want && page.next);
      return page;
    },
    { shouldRetryOnError: false, revalidateFirstPage: false },
  );
  // A project's strip and Files follow its tasks: when one of them makes something, read again. The room's task list
  // is the one the Tasks pane already reads (same SWR key), so this costs no request.
  const roomId = filter?.roomId;
  const roomScope = useMemo(() => (roomId ? { kind: "room" as const, roomId } : null), [roomId]);
  const { data: roomTasks } = useTaskList(tasks, roomScope);
  const made = roomTasks?.tasks.reduce((n, t) => n + (t.outputs?.length ?? 0), 0);
  const seen = useRef<number | undefined>(undefined);
  // Mounting over a cached read re-reads it: the strip unmounts while the pane shows a task, so a task that made
  // something meanwhile was never counted here (dev walk 12, W12-6). SWR's own mount revalidation skips a cached page.
  const cached = useRef(data !== undefined);
  useEffect(() => { if (cached.current) { cached.current = false; void mutate(); } }, [mutate]);
  useEffect(() => {
    if (made === undefined) return;
    if (seen.current !== undefined && seen.current !== made) void mutate();
    seen.current = made;
  }, [made, mutate]);

  const outputs = useMemo(() => data?.flatMap((p) => p.outputs) ?? [], [data]);
  const last = data?.[data.length - 1];
  return {
    outputs,
    loading: isLoading,
    /** How many outputs the room's task list already shows, before this read lands (0 without a room). */
    expected: made ?? 0,
    error: !!error && !data,
    retry: () => void mutate(),
    hasMore: !!last?.next,
    loadingMore: isValidating && !!data && size > data.length,
    loadMore: () => void setSize(size + 1),
  };
}
