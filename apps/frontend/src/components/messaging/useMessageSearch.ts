"use client";

import { useEffect, useState } from "react";
import type { MessageSearchHit } from "@lightbulb/shared";
import { useLiveEvents } from "@/hooks/useLiveEvents";

export type SearchState = "idle" | "loading" | "warming" | "error" | "done";

const DEBOUNCE_MS = 250;
const RETRY_MS = 3_000;

/** One search box's results: warm on open, debounce, one retry while search wakes. */
export function useMessageSearch(
  query: string,
  { enabled, roomId, limit, minLength = 1 }: { enabled: boolean; roomId?: string; limit: number; minLength?: number },
): { hits: MessageSearchHit[]; state: SearchState } {
  const { messaging } = useLiveEvents();
  const term = query.trim();
  const active = enabled && term.length >= minLength;
  const key = `${roomId ?? ""}\u0000${term}`;
  const [result, setResult] = useState<{ key: string; hits: MessageSearchHit[]; state: SearchState }>({ key: "", hits: [], state: "idle" });

  useEffect(() => {
    if (enabled) void messaging.warmSearch().catch(() => {});
  }, [enabled, messaging]);

  useEffect(() => {
    if (!active) return;
    let live = true;
    let retry: ReturnType<typeof setTimeout> | undefined;
    const run = (retried: boolean) => messaging.searchMessages(term, { roomId, limit })
      .then((response) => { if (live) setResult({ key, hits: response.hits, state: "done" }); })
      .catch((error: { status?: number }) => {
        if (!live) return;
        const warming = error?.status === 503;
        setResult({ key, hits: [], state: warming ? "warming" : "error" });
        if (warming && !retried) retry = setTimeout(() => void run(true), RETRY_MS);
      });
    const timer = setTimeout(() => void run(false), DEBOUNCE_MS);
    return () => { live = false; clearTimeout(timer); clearTimeout(retry); };
  }, [active, key, term, roomId, limit, messaging]);

  if (!active) return { hits: [], state: "idle" };
  return result.key === key ? { hits: result.hits, state: result.state } : { hits: [], state: "loading" };
}
