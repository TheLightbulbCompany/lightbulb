"use client";

import { useRef, useState } from "react";
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
  // Guards the editor's close: Enter (and Escape) unmount the focused input,
  // which fires a synchronous blur in real browsers whose handler still sees
  // the pre-close render's state — without the guard, Enter would commit
  // twice and Escape's blur would commit the value the user just canceled.
  const editorClosedRef = useRef(false);

  // A just-created session has no sessions.list row until its first message
  // materializes it server-side. Surface it as a placeholder row so clicking
  // "New session" visibly selects something — without this, on an agent whose
  // transcript is already blank the click looks like a no-op.
  const activeIsUnmaterialized =
    activeSessionId !== null && !sessions.some((s) => s.sessionId === activeSessionId);

  const openEditor = (s: AgentSession): void => {
    editorClosedRef.current = false;
    // Seed from the untruncated title — s.label is display-truncated and
    // committing it would persist the ellipsis'd string as the real name.
    setEditing({ key: s.key, value: s.rawLabel });
  };

  const cancelRename = (): void => {
    editorClosedRef.current = true;
    setEditing(null);
  };

  const commitRename = (s: AgentSession): void => {
    if (editorClosedRef.current) return;
    editorClosedRef.current = true;
    const value = editing?.value.trim() ?? "";
    setEditing(null);
    if (value && value !== s.rawLabel) onRename?.(s, value);
  };

  return (
    <div className="session-list">
      {activeIsUnmaterialized && (
        /* Static indicator; disabled so keyboard users don't land on a no-op. */
        <button type="button" className="session-item active" aria-current="true" disabled>
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
                  if (e.key === "Escape") cancelRename();
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
                      openEditor(s);
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
