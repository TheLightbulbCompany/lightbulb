"use client";

import { useState } from "react";
import { Pencil, Plus } from "lucide-react";
import type { AgentSession } from "@/hooks/useAgentSessions";

interface SessionListProps {
  sessions: AgentSession[];
  activeSessionId: string | null;
  onSelect: (sessionId: string | null) => void;
  onNew: () => void;
  /** Persist a user-set name for a materialized session (sessions.patch {label}). */
  onRename?: (session: AgentSession, label: string) => void;
}

/**
 * Nested per-agent session list shown under the active agent in the sidebar.
 * Presentational only — the caller (ChatLayout) supplies sessions from
 * useAgentSessions and select/new/rename handlers.
 */
export function SessionList({
  sessions,
  activeSessionId,
  onSelect,
  onNew,
  onRename,
}: SessionListProps): React.JSX.Element {
  const [editing, setEditing] = useState<{ key: string; value: string } | null>(null);

  // A just-created session has no sessions.list row until its first message
  // materializes it server-side. Surface it as a placeholder row so clicking
  // "New session" visibly selects something — without this, on an agent whose
  // transcript is already blank the click looks like a no-op.
  const activeIsUnmaterialized =
    activeSessionId !== null && !sessions.some((s) => s.sessionId === activeSessionId);

  const commitRename = (s: AgentSession): void => {
    const value = editing?.value.trim() ?? "";
    setEditing(null);
    if (value && value !== s.label) onRename?.(s, value);
  };

  return (
    <div className="session-list">
      {activeIsUnmaterialized && (
        <button type="button" className="session-item active" aria-current="true">
          <span className="session-label">New session</span>
        </button>
      )}
      {sessions.map((s) => {
        const isActive = s.sessionId === activeSessionId;
        const isEditing = editing?.key === s.key;
        return (
          <div key={s.key} className={`session-row${isActive ? " active" : ""}`}>
            {isEditing ? (
              <input
                className="session-rename-input"
                value={editing.value}
                autoFocus
                aria-label={`Rename session ${s.label}`}
                onChange={(e) => setEditing({ key: s.key, value: e.target.value })}
                onKeyDown={(e) => {
                  if (e.key === "Enter") commitRename(s);
                  if (e.key === "Escape") setEditing(null);
                }}
                onBlur={() => commitRename(s)}
                onClick={(e) => e.stopPropagation()}
              />
            ) : (
              <>
                <button
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
                {/* General (sessionId null) keeps its fixed identity — only
                    named sessions are renameable. */}
                {onRename && s.sessionId !== null && (
                  <button
                    type="button"
                    className="session-rename"
                    aria-label={`Rename ${s.label}`}
                    title="Rename session"
                    onClick={(e) => {
                      e.stopPropagation();
                      setEditing({ key: s.key, value: s.label });
                    }}
                  >
                    <Pencil size={11} />
                  </button>
                )}
              </>
            )}
          </div>
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
