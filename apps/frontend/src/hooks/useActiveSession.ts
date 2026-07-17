"use client";

import { useCallback, useSyncExternalStore } from "react";

// Module-level per-agent active-session store. Survives component unmounts and
// is shared across React subtrees (the sidebar SessionList in ChatLayout and
// the transcript in AgentChatWindow live in different subtrees but must agree
// on which session is active). Mirrors the _messageCache module-store pattern
// already used in useAgentChat. `null` == the legacy "General" session.
//
// The Map alone is wiped on a full page refresh, which would reset a user who
// was in a NAMED session back to General on every reload. To survive a refresh
// the store is BACKED by localStorage, keyed per agent: it hydrates lazily from
// localStorage on the first read for an agent (so a fresh module load restores
// the last session) and mirrors every write back. `null` (General) is stored as
// the ABSENCE of a key, so selecting General clears the persisted named session
// and a subsequent refresh restores General rather than a stale named session.
const _activeByAgent = new Map<string, string | null>();
const _listeners = new Set<() => void>();

const STORAGE_PREFIX = "isol8:activeSession:";

function storageKey(agentId: string): string {
  return `${STORAGE_PREFIX}${agentId}`;
}

// localStorage is unavailable during SSR (no `window`) and can throw in private
// mode / when disabled — every access is guarded so the in-memory Map still
// works as a session-scoped fallback.
function readStoredSession(agentId: string): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(storageKey(agentId));
  } catch {
    return null;
  }
}

function writeStoredSession(agentId: string, sessionId: string | null): void {
  if (typeof window === "undefined") return;
  try {
    if (sessionId) {
      window.localStorage.setItem(storageKey(agentId), sessionId);
    } else {
      // General == no stored key, so a refresh restores General rather than a
      // stale named session.
      window.localStorage.removeItem(storageKey(agentId));
    }
  } catch {
    // Private mode / quota / disabled storage — the in-memory Map already holds
    // the value for this session; persistence across refresh is best-effort.
  }
}

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
  // Lazy-hydrate from localStorage on the first read for this agent so a full
  // page refresh (fresh module → empty Map) restores the last active session.
  // Memoize into the Map so useSyncExternalStore's getSnapshot stays value-
  // stable (identical primitive) across subsequent reads.
  if (!_activeByAgent.has(agentId)) {
    _activeByAgent.set(agentId, readStoredSession(agentId));
  }
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
  // Persist every write (newSession, selectSession, selectSession(null)) so the
  // choice survives a refresh.
  writeStoredSession(agentId, sessionId);
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
