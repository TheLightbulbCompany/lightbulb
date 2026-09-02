"use client";

// Full-text search over the current agent's session transcripts (OpenClaw
// 8.1's `sessions.search` RPC). Presentational shell over useSessionSearch;
// clicking a hit resolves the OpenClaw session key to this app's sessionId
// shape (see @lightbulb/shared's sessionIdFromKey — the ONE cross-client copy
// of this parsing, already used by mobile) and hands the caller that plus the
// hit's messageId — the caller wires the switch (see WorkspaceSidebar's
// `selectSession`). Highlighting reuses the Teams search HighlightedText
// component (text-node rendering, never dangerouslySetInnerHTML); this is the
// one client that has to compute its own match ranges, since sessions.search
// returns a plain snippet string with no server-side highlight ranges.

import { useEffect, useMemo, useState } from "react";
import { Search } from "lucide-react";
import { sessionIdFromKey } from "@lightbulb/shared";
import { HighlightedText } from "@/components/teams/search/HighlightedText";
import type { AgentSession } from "@/hooks/useAgentSessions";
import { useSessionSearch, type SessionSearchHit } from "@/hooks/useSessionSearch";
import { notifyApiError } from "@/lib/notify";

export interface SessionSearchProps {
  agentId: string | null;
  /** The current agent's known sessions — used to resolve a real session
   *  title for a hit's group heading instead of the raw key tail. */
  sessions: AgentSession[];
  /** sessionId is already resolved to this app's shape (null == General) —
   *  callers pass it straight to `selectSession`. */
  onSelect: (sessionId: string | null, messageId: string) => void;
}

function timeAgo(ms: number): string {
  const diff = Date.now() - ms;
  const MIN = 60_000;
  const HOUR = 3_600_000;
  const DAY = 86_400_000;
  if (diff < MIN) return "just now";
  if (diff < HOUR) return `${Math.floor(diff / MIN)}m ago`;
  if (diff < DAY) return `${Math.floor(diff / HOUR)}h ago`;
  return `${Math.floor(diff / DAY)}d ago`;
}

/** Case-insensitive, whole-term match ranges for HighlightedText — sessions.search
 *  gives us a plain snippet string with no server-computed highlight ranges. */
function queryMatchRanges(text: string, query: string): { start: number; end: number }[] {
  const terms = Array.from(new Set(query.trim().split(/\s+/).filter((t) => t.length > 0)));
  if (terms.length === 0) return [];
  const lowerText = text.toLowerCase();
  const ranges: { start: number; end: number }[] = [];
  for (const term of terms) {
    const lowerTerm = term.toLowerCase();
    let from = 0;
    for (;;) {
      const idx = lowerText.indexOf(lowerTerm, from);
      if (idx === -1) break;
      ranges.push({ start: idx, end: idx + term.length });
      from = idx + term.length;
    }
  }
  return ranges;
}

function sessionLabel(sessionKey: string, sessions: AgentSession[]): string {
  const known = sessions.find((s) => s.key === sessionKey);
  if (known) return known.label;
  const sessionId = sessionIdFromKey(sessionKey);
  if (sessionId === null) return "General";
  return `Session ${sessionId.slice(0, 8)}`;
}

export function SessionSearch({ agentId, sessions, onSelect }: SessionSearchProps): React.JSX.Element {
  const [query, setQuery] = useState("");
  // The gateway scopes the scan to these keys — the sidebar rows we already
  // have. Memoized so the RPC key is stable across renders of the same list.
  const sessionKeys = useMemo(() => sessions.map((s) => s.key), [sessions]);
  const { isActive, effectiveQuery, results, isLoading, indexing, truncated, error } =
    useSessionSearch(agentId, query, sessionKeys);

  useEffect(() => {
    if (error) notifyApiError(error, "Search failed", { dedupeKey: "session-search" });
  }, [error]);

  const groups = useMemo(() => {
    const bySession = new Map<string, SessionSearchHit[]>();
    for (const hit of results) {
      const list = bySession.get(hit.sessionKey);
      if (list) list.push(hit);
      else bySession.set(hit.sessionKey, [hit]);
    }
    return Array.from(bySession.entries());
  }, [results]);

  return (
    <div className="session-search">
      <div className="session-search__box">
        <Search size={12} aria-hidden="true" className="session-search__icon" />
        <input
          type="search"
          className="session-search__input"
          placeholder="Search conversations"
          aria-label="Search conversations"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>
      {isActive && (
        <div className="session-search__results">
          {isLoading ? (
            <div className="session-search__hint">Searching…</div>
          ) : indexing ? (
            <div className="session-search__hint">
              Indexing this agent&apos;s conversations for the first time — try again in a moment.
            </div>
          ) : results.length === 0 ? (
            <div className="session-search__hint">No matches</div>
          ) : (
            <>
              {groups.map(([sessionKey, hits]) => (
                <div key={sessionKey} className="session-search__group">
                  <div className="session-search__group-label">
                    {sessionLabel(sessionKey, sessions)}
                  </div>
                  {hits.map((hit) => (
                    <button
                      key={hit.messageId}
                      type="button"
                      className="session-search__hit"
                      onClick={() => onSelect(sessionIdFromKey(hit.sessionKey), hit.messageId)}
                    >
                      <span className="session-search__hit-meta">
                        <span className="session-search__hit-role">
                          {hit.role === "user" ? "You" : "Agent"}
                        </span>
                        <span className="session-search__hit-time">{timeAgo(hit.timestamp)}</span>
                      </span>
                      <HighlightedText
                        text={hit.snippet}
                        highlights={queryMatchRanges(hit.snippet, effectiveQuery)}
                        className="session-search__hit-snippet"
                      />
                    </button>
                  ))}
                </div>
              ))}
              {truncated && (
                <div className="session-search__hint">Showing first {results.length} matches</div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
