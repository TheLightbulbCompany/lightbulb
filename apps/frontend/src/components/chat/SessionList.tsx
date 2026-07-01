"use client";

import { Plus } from "lucide-react";
import type { AgentSession } from "@/hooks/useAgentSessions";

interface SessionListProps {
  sessions: AgentSession[];
  activeSessionId: string | null;
  onSelect: (sessionId: string | null) => void;
  onNew: () => void;
}

/**
 * Nested per-agent session list shown under the active agent in the sidebar.
 * Presentational only — the caller (ChatLayout) supplies sessions from
 * useAgentSessions and select/new handlers from useActiveSession.
 */
export function SessionList({
  sessions,
  activeSessionId,
  onSelect,
  onNew,
}: SessionListProps): React.JSX.Element {
  return (
    <div className="session-list">
      {sessions.map((s) => {
        const isActive = s.sessionId === activeSessionId;
        return (
          <button
            key={s.key}
            type="button"
            className={`session-item${isActive ? " active" : ""}`}
            aria-current={isActive ? "true" : undefined}
            onClick={(e) => {
              e.stopPropagation();
              onSelect(s.sessionId);
            }}
          >
            <span className="session-label">{s.label}</span>
          </button>
        );
      })}
      <button
        type="button"
        className="session-new"
        aria-label="New session"
        onClick={(e) => {
          e.stopPropagation();
          onNew();
        }}
      >
        <Plus size={13} />
        <span>New session</span>
      </button>
    </div>
  );
}
