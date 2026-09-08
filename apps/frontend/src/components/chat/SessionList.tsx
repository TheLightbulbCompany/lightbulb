"use client";

import { useRef, useState } from "react";
import { AlertTriangle, Archive, Pencil, Pin, Plus } from "lucide-react";
import { isUnhappyRun, type AgentSession } from "@/hooks/useAgentSessions";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";

/** Plain words for the three bad terminal outcomes; keyed to the row's status. */
const RUN_OUTCOME_LABEL = {
  failed: "Last run failed",
  killed: "Last run was stopped",
  timeout: "Last run timed out",
} as const;

interface SessionListProps {
  sessions: AgentSession[];
  activeSessionId: string | null;
  onSelect: (sessionId: string | null) => void;
  onNew: () => void;
  /** Persist a user-set name for a materialized session (sessions.patch {label}). */
  onRename?: (session: AgentSession, label: string) => void;
  /**
   * Archive a session out of the rail (sessions.patch {archived:true}).
   *
   * This is the rail's only removal action, deliberately. It is reversible from
   * the archived drawer, so it needs no confirm; permanent deletion lives in
   * that drawer, one step further in, behind its own dialog. An owner should
   * not be able to destroy a conversation from a hover icon.
   */
  onArchive?: (session: AgentSession) => void;
  /** Toggle the owner's sort pin (sessions.patch {pinned}). */
  onPin?: (session: AgentSession, next: boolean) => void;
  /** Rendered after "New session" — the archived drawer, supplied by the shell. */
  archivedSlot?: React.ReactNode;
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
  onArchive,
  onPin,
  archivedSlot,
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
          <span className="session-line">
            <span className="session-label">New session</span>
          </span>
        </button>
      )}
      {sessions.map((s) => {
        const isActive = s.sessionId === activeSessionId;
        const isEditing = editing?.key === s.key;
        const outcome = isUnhappyRun(s.status) ? RUN_OUTCOME_LABEL[s.status] : null;
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
                  <span className="session-line">
                    {outcome && (
                      /* A failed routine used to look identical to one that
                         worked. The marker carries its own label and hover
                         text, so the signal is never colour alone. */
                      <span
                        className="session-outcome"
                        role="img"
                        aria-label={outcome}
                        title={s.lastRunError || outcome}
                      >
                        <AlertTriangle size={11} aria-hidden />
                      </span>
                    )}
                    <span className="session-label">{s.label}</span>
                    {s.updatedAt ? (
                      <time className="session-when" dateTime={new Date(s.updatedAt).toISOString()}>
                        {timeAgo(new Date(s.updatedAt))}
                      </time>
                    ) : null}
                  </span>
                  {/* One line of what was last said. Absent on a session whose
                      transcript has no visible user/assistant message yet. */}
                  {s.preview ? <span className="session-preview">{s.preview}</span> : null}
                </button>
                {/* The legacy row (sessionId null) keeps its fixed identity: it
                    is the agent's main session, which the container refuses to
                    archive, and it holds platform-written history nobody chose
                    to name. Pin/rename/archive are for real sessions only. */}
                {onPin && s.sessionId !== null && (
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <button
                        type="button"
                        /* A pinned row shows its pin at rest — a hover-only
                           control cannot explain why that row sits on top. */
                        className={`session-rename${s.pinned ? " session-action--on" : ""}`}
                        aria-label={s.pinned ? `Unpin ${s.label}` : `Pin ${s.label}`}
                        aria-pressed={s.pinned ? "true" : "false"}
                        onClick={(e) => {
                          e.stopPropagation();
                          onPin(s, !s.pinned);
                        }}
                      >
                        <Pin size={12} />
                      </button>
                    </TooltipTrigger>
                    <TooltipContent>{s.pinned ? "Unpin session" : "Pin to top"}</TooltipContent>
                  </Tooltip>
                )}
                {onRename && s.sessionId !== null && (
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <button
                        type="button"
                        className="session-rename"
                        aria-label={`Rename ${s.label}`}
                        onClick={(e) => {
                          e.stopPropagation();
                          openEditor(s);
                        }}
                      >
                        <Pencil size={12} />
                      </button>
                    </TooltipTrigger>
                    <TooltipContent>Rename session</TooltipContent>
                  </Tooltip>
                )}
                {onArchive && s.sessionId !== null && (
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <button
                        type="button"
                        className="session-rename"
                        aria-label={`Archive ${s.label}`}
                        onClick={(e) => {
                          e.stopPropagation();
                          onArchive(s);
                        }}
                      >
                        <Archive size={12} />
                      </button>
                    </TooltipTrigger>
                    <TooltipContent>Archive session</TooltipContent>
                  </Tooltip>
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
        <Plus size={12} />
        <span>New session</span>
      </button>
      {archivedSlot}
    </div>
  );
}
