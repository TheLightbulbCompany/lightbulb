"use client";

import { useState } from "react";
import { ArchiveRestore, ChevronDown, ChevronRight, Trash2 } from "lucide-react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { useAgentSessions, type AgentSession } from "@/hooks/useAgentSessions";

interface ArchivedSessionsProps {
  agentId: string | null;
  userId: string | null;
  /** Ask to permanently delete — the caller owns the confirm + sessions.delete. */
  onDelete: (session: AgentSession) => void;
}

/**
 * The other half of archive: without somewhere to see and restore archived
 * sessions, archiving is a write-only trapdoor and the owner is right back to
 * "the only way out is delete".
 *
 * Collapsed by default, and it fetches NOTHING until opened — passing a null
 * agentId nulls the SWR key. That matters on a 1-vCPU container: this is a
 * second `sessions.list` with the same bounded transcript projections as the
 * rail, and most sessions are never archived. Opening it costs one read; the
 * rail's own read is untouched either way.
 */
export function ArchivedSessions({
  agentId,
  userId,
  onDelete,
}: ArchivedSessionsProps): React.JSX.Element {
  const [open, setOpen] = useState(false);
  const {
    sessions,
    isLoading,
    setArchived,
  } = useAgentSessions(open ? agentId : null, userId, true);

  return (
    <div className="session-archived">
      <button
        type="button"
        className="session-new session-archived-toggle"
        aria-expanded={open}
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
      >
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        <span>Archived{open && sessions.length > 0 ? ` (${sessions.length})` : ""}</span>
      </button>
      {open && isLoading && <p className="session-archived-note">Loading…</p>}
      {open && !isLoading && sessions.length === 0 && (
        <p className="session-archived-note">Nothing archived</p>
      )}
      {open &&
        sessions.map((s) => (
          /* Not selectable: an archived session is out of the rail, and
             selecting one would leave the chat pointed at a row the rail no
             longer lists. Restore it first. */
          <div key={s.key} className="session-row session-row--archived">
            <span className="session-item">
              <span className="session-line">
                <span className="session-label">{s.label}</span>
                {s.updatedAt ? (
                  <time className="session-when" dateTime={new Date(s.updatedAt).toISOString()}>
                    {timeAgo(new Date(s.updatedAt))}
                  </time>
                ) : null}
              </span>
            </span>
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  className="session-rename"
                  aria-label={`Restore ${s.label}`}
                  onClick={(e) => {
                    e.stopPropagation();
                    void setArchived(s.key, false);
                  }}
                >
                  <ArchiveRestore size={12} />
                </button>
              </TooltipTrigger>
              <TooltipContent>Restore to the session list</TooltipContent>
            </Tooltip>
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  className="session-rename session-delete"
                  aria-label={`Delete ${s.label} permanently`}
                  onClick={(e) => {
                    e.stopPropagation();
                    onDelete(s);
                  }}
                >
                  <Trash2 size={12} />
                </button>
              </TooltipTrigger>
              <TooltipContent>Delete permanently</TooltipContent>
            </Tooltip>
          </div>
        ))}
    </div>
  );
}
