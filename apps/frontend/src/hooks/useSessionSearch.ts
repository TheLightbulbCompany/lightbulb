"use client";

import { useEffect, useState } from "react";
import { useGatewayRpc } from "@/hooks/useGatewayRpc";

// OpenClaw 8.1's `sessions.search` scans every session store for ONE agent per
// call (gateway-protocol sessions-search-scope.ts refuses more than one), so
// an un-debounced, single-character query would hammer the container on every
// keystroke. Gate below this length and debounce the rest.
const MIN_QUERY_LENGTH = 2;
const DEBOUNCE_MS = 300;
const RESULT_LIMIT = 20;

/** One `sessions.search` hit (gateway-protocol `SessionsSearchHitSchema`). */
export interface SessionSearchHit {
  sessionKey: string;
  sessionId: string;
  messageId: string;
  role: "user" | "assistant";
  timestamp: number;
  snippet: string;
  score: number;
}

interface SessionsSearchResult {
  results: SessionSearchHit[];
  indexing?: boolean;
  truncated?: boolean;
}

export interface UseSessionSearchReturn {
  /** True once the debounced query has cleared the minimum-length gate — the
   *  caller uses this to decide whether to show a results panel at all. */
  isActive: boolean;
  /** The trimmed query actually sent to the gateway (== debounced, not the
   *  raw keystroke value) — use this, not the live input, to compute
   *  highlight ranges against `results`, which lag the raw input by design. */
  effectiveQuery: string;
  results: SessionSearchHit[];
  isLoading: boolean;
  /** A first-use transcript reconcile is still running; retry shortly. */
  indexing: boolean;
  /** More matches exist than `results` — the gateway capped the response. */
  truncated: boolean;
  error: Error | undefined;
}

/**
 * Full-text search over one agent's session transcripts (OpenClaw 8.1's
 * `sessions.search` RPC). Debounces the raw input and never fires under
 * MIN_QUERY_LENGTH chars.
 */
export function useSessionSearch(agentId: string | null, query: string): UseSessionSearchReturn {
  const [debounced, setDebounced] = useState("");

  useEffect(() => {
    const handle = setTimeout(() => setDebounced(query), DEBOUNCE_MS);
    return () => clearTimeout(handle);
  }, [query]);

  const trimmed = debounced.trim();
  const isActive = Boolean(agentId) && trimmed.length >= MIN_QUERY_LENGTH;
  const method = isActive ? "sessions.search" : null;
  const params = isActive ? { agentId: agentId as string, query: trimmed, limit: RESULT_LIMIT } : undefined;

  const { data, error, isLoading } = useGatewayRpc<SessionsSearchResult>(method, params);

  return {
    isActive,
    effectiveQuery: trimmed,
    results: isActive ? data?.results ?? [] : [],
    isLoading: isActive && isLoading,
    indexing: isActive && Boolean(data?.indexing),
    truncated: isActive && Boolean(data?.truncated),
    error,
  };
}
