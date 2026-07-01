"use client";

import { useMemo } from "react";
import { useGatewayRpc } from "@/hooks/useGatewayRpc";

// Mirror of the backend guard (websocket_chat.py _SESSION_ID_RE): a strict
// lowercase RFC-4122 v4. Anything else in the trailing segment is not a
// session we minted and must never be surfaced as selectable.
const SESSION_ID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export interface AgentSession {
  /** Trailing UUID for a real session; null == the legacy "General" session. */
  sessionId: string | null;
  /** The full OpenClaw session key. */
  key: string;
  label: string;
  updatedAt?: number;
  totalTokens?: number;
}

interface RawSessionRow {
  key: string;
  derivedTitle?: string;
  title?: string;
  lastMessage?: { text?: string };
  updatedAt?: number;
  totalTokens?: number;
}

function labelFor(row: RawSessionRow, sessionId: string): string {
  const raw = (row.derivedTitle || row.title || row.lastMessage?.text || "").trim();
  if (raw) return raw.length > 48 ? `${raw.slice(0, 48)}…` : raw;
  // Never blank — fall back to a short id so the row is always clickable.
  return `Session ${sessionId.slice(0, 8)}`;
}

/**
 * Per-agent session list for the sidebar. The backend sessions.list proxy has
 * already dropped paperclip/cron/channel/other-member rows; here we keep only
 * 4-part `agent:{agentId}:{userId}:{uuid}` rows for THIS agent + user, newest
 * first. The legacy General (3-part) row is added by useAgentSessions' Task-12
 * extension.
 */
export function useAgentSessions(
  agentId: string | null,
  userId: string | null,
): { sessions: AgentSession[]; isLoading: boolean; mutate: () => Promise<unknown> } {
  const { data, isLoading, mutate } = useGatewayRpc<
    { sessions?: RawSessionRow[] } | RawSessionRow[]
  >(
    agentId ? "sessions.list" : null,
    {
      includeGlobal: false,
      includeUnknown: false,
      includeDerivedTitles: true,
      includeLastMessage: true,
      limit: 50,
    },
    { dedupingInterval: 10_000 },
  );

  const sessions = useMemo<AgentSession[]>(() => {
    if (!agentId || !userId) return [];
    const rows: RawSessionRow[] = Array.isArray(data) ? data : data?.sessions ?? [];
    const out: AgentSession[] = [];
    for (const row of rows) {
      if (typeof row?.key !== "string") continue;
      const parts = row.key.split(":");
      if (parts.length !== 4) continue;
      if (parts[0] !== "agent" || parts[1] !== agentId || parts[2] !== userId) continue;
      const sessionId = parts[3];
      if (!SESSION_ID_RE.test(sessionId)) continue;
      out.push({
        sessionId,
        key: row.key,
        label: labelFor(row, sessionId),
        updatedAt: row.updatedAt,
        totalTokens: row.totalTokens,
      });
    }
    out.sort((a, b) => (b.updatedAt ?? 0) - (a.updatedAt ?? 0));
    return out;
  }, [data, agentId, userId]);

  return { sessions, isLoading, mutate };
}
