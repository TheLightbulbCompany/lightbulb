// apps/frontend/src/components/teams/issues/IssueHeader.tsx

// Ported from upstream Paperclip's pages/IssueDetail.tsx header section
// (paperclip/ui/src/pages/IssueDetail.tsx) (MIT, (c) 2025 Paperclip AI).
// A calm editorial masthead: muted mono identifier + prominent inline-editable
// title, nothing else. Status/priority/assignee live solely in the right-rail
// Properties panel so each field has a single editing surface (matching
// upstream, where the header carries no pickers).

"use client";

import { useState, useEffect, useRef } from "react";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { workModeMetaFor } from "@/components/teams/shared/lib/workModeMeta";
import type { Issue } from "@/components/teams/shared/types";
import { notifyApiError } from "@/lib/notify";

export interface IssueHeaderProps {
  issue: Issue;
  onTitleSave: (title: string) => Promise<void>;
}

export function IssueHeader({ issue, onTitleSave }: IssueHeaderProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(issue.title);
  const [saving, setSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  // Sync draft when issue.title changes externally (e.g. mutation resolves
  // and the parent re-renders with the new title).
  useEffect(() => {
    setDraft(issue.title);
  }, [issue.title]);

  useEffect(() => {
    if (editing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [editing]);

  const save = async () => {
    const trimmed = draft.trim();
    if (!trimmed || trimmed === issue.title) {
      setDraft(issue.title);
      setEditing(false);
      return;
    }
    setSaving(true);
    try {
      await onTitleSave(trimmed);
      setEditing(false);
    } catch (err) {
      notifyApiError(err, "Couldn't save the task title");
    } finally {
      setSaving(false);
    }
  };

  const cancel = () => {
    setDraft(issue.title);
    setEditing(false);
  };

  // Upstream's legacy header badge: shown only off the default mode.
  const workModeMeta =
    issue.workMode && issue.workMode !== "standard"
      ? workModeMetaFor(issue.workMode)
      : null;
  const WorkModeIcon = workModeMeta?.icon;

  return (
    <div className="flex items-baseline gap-3">
      {issue.identifier && (
        <span
          className="text-xs font-mono shrink-0"
          style={{ color: "var(--midnight-muted)" }}
        >
          {issue.identifier}
        </span>
      )}

      {editing ? (
        <input
          ref={inputRef}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => void save()}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              void save();
            } else if (e.key === "Escape") {
              e.preventDefault();
              cancel();
            }
          }}
          className="font-editorial-serif flex-1 min-w-0 bg-transparent text-2xl font-medium"
          style={{
            borderBottom: "1px solid var(--edge)",
            color: "var(--midnight)",
          }}
          disabled={saving}
        />
      ) : (
        <button
          type="button"
          onClick={() => setEditing(true)}
          className={cn(
            "font-editorial-serif flex-1 min-w-0 text-left text-2xl font-medium rounded-sm px-1 -mx-1 transition-colors",
            saving && "opacity-50",
          )}
          style={{ color: "var(--midnight)" }}
          onMouseEnter={(e) => {
            e.currentTarget.style.background = "var(--cream-deep)";
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.background = "transparent";
          }}
        >
          {issue.title}
        </button>
      )}
      {workModeMeta && WorkModeIcon && (
        <Badge
          variant="outline"
          className={cn("self-center", workModeMeta.classes.badge)}
          title={`This task is in ${workModeMeta.label.toLowerCase()}.`}
        >
          <WorkModeIcon className="h-3 w-3" aria-hidden />
          {workModeMeta.label}
        </Badge>
      )}
    </div>
  );
}
