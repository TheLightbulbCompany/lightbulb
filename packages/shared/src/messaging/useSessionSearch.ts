import { useEffect, useState } from "react";
import type { MessagingClient } from "./client";
import type { SessionSearchHit } from "./types";

export type SearchState = "idle" | "loading" | "warming" | "error" | "done";

const DEBOUNCE_MS = 250;
const RETRY_MS = 3_000;

/** One session-search box's results: warm on open, debounce, one retry while search wakes
 *  (the web message search's rules, for web and phone). */
export function useSessionSearch(
  client: Pick<MessagingClient, "searchSessions" | "warmSearch">,
  query: string,
  { enabled, limit = 30, minLength = 2 }: { enabled: boolean; limit?: number; minLength?: number },
): { hits: SessionSearchHit[]; state: SearchState } {
  const term = query.trim();
  const active = enabled && term.length >= minLength;
  const [result, setResult] = useState<{ key: string; hits: SessionSearchHit[]; state: SearchState }>({ key: "", hits: [], state: "idle" });

  useEffect(() => {
    if (enabled) void client.warmSearch().catch(() => {});
  }, [enabled, client]);

  useEffect(() => {
    if (!active) return;
    let live = true;
    let retry: ReturnType<typeof setTimeout> | undefined;
    const run = (retried: boolean) => client.searchSessions(term, { limit })
      .then((response) => { if (live) setResult({ key: term, hits: response.hits, state: "done" }); })
      .catch((error: { status?: number }) => {
        if (!live) return;
        const warming = error?.status === 503;
        // A 503 on the retry is a real failure: never say "starting up" forever.
        setResult({ key: term, hits: [], state: warming && !retried ? "warming" : "error" });
        if (warming && !retried) retry = setTimeout(() => void run(true), RETRY_MS);
      });
    const timer = setTimeout(() => void run(false), DEBOUNCE_MS);
    return () => { live = false; clearTimeout(timer); clearTimeout(retry); };
  }, [active, term, limit, client]);

  if (!active) return { hits: [], state: "idle" };
  return result.key === term ? { hits: result.hits, state: result.state } : { hits: [], state: "loading" };
}
