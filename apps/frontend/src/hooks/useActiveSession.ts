"use client";

import { useCallback, useSyncExternalStore } from "react";

// Module-level per-agent active-session store. Survives component unmounts and
// is shared across React subtrees (the sidebar SessionList in ChatLayout and
// the transcript in AgentChatWindow live in different subtrees but must agree
// on which session is active). Mirrors the _messageCache module-store pattern
// already used in useAgentChat. `null` == the legacy "General" session.
const _activeByAgent = new Map<string, string | null>();
const _listeners = new Set<() => void>();

function emit(): void {
  _listeners.forEach((l) => l());
}

function subscribe(listener: () => void): () => void {
  _listeners.add(listener);
  return () => {
    _listeners.delete(listener);
  };
}

function read(agentId: string | null): string | null {
  if (!agentId) return null;
  return _activeByAgent.get(agentId) ?? null;
}

// Non-hook read of the module store for callers that need the active session of
// an arbitrary agent outside a component's own useActiveSession subscription —
// notably RunNotifications' desktop path, which must resolve a session per
// visible chat agent and so cannot call the hook in a loop. Returns null
// (General) when the agent has never selected a session.
export function getActiveSessionId(agentId: string): string | null {
  return read(agentId);
}

function write(agentId: string, sessionId: string | null): void {
  _activeByAgent.set(agentId, sessionId);
  emit();
}

export interface UseActiveSessionReturn {
  activeSessionId: string | null;
  /** Mint a fresh lowercase UUIDv4, set it active, and return it. */
  newSession: () => string;
  selectSession: (sessionId: string | null) => void;
}

export function useActiveSession(
  agentId: string | null,
): UseActiveSessionReturn {
  const activeSessionId = useSyncExternalStore(
    subscribe,
    () => read(agentId),
    () => read(agentId),
  );

  const newSession = useCallback((): string => {
    // crypto.randomUUID() is RFC-4122 v4 and already lowercase — matches the
    // backend _SESSION_ID_RE guard exactly, so the 4-part key is accepted.
    const id = crypto.randomUUID();
    if (agentId) write(agentId, id);
    return id;
  }, [agentId]);

  const selectSession = useCallback(
    (sessionId: string | null): void => {
      if (agentId) write(agentId, sessionId);
    },
    [agentId],
  );

  return { activeSessionId, newSession, selectSession };
}
