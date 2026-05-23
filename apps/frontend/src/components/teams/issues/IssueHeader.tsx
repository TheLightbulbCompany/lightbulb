// apps/frontend/src/components/teams/issues/IssueHeader.tsx

// Ported from upstream Paperclip's pages/IssueDetail.tsx header section
// (paperclip/ui/src/pages/IssueDetail.tsx) (MIT, (c) 2025 Paperclip AI).
// v1: identifier + title (inline editable) + status + priority pickers.
// Drops assignee picker, parent issue, watcher list, run actions, plugin slots.

"use client";

import { useState, useEffect, useRef } from "react";
import { StatusIcon } from "@/components/teams/shared/components/StatusIcon";
import { PriorityIcon } from "@/components/teams/shared/components/PriorityIcon";
import { cn } from "@/lib/utils";
import type {
  Issue,
  IssueStatus,
  IssuePriority,
  CompanyAgent,
} from "@/components/teams/shared/types";
import { AssigneePicker } from "./pickers/AssigneePicker";

export interface IssueHeaderProps {
  issue: Issue;
  agents?: CompanyAgent[];
  onTitleSave: (title: string) => Promise<void>;
  onStatusChange: (status: IssueStatus) => Promise<void>;
  onPriorityChange: (priority: IssuePriority) => Promise<void>;
  onAssigneeChange: (agentId: string | null) => Promise<void>;
}

export function IssueHeader({
  issue,
  agents = [],
  onTitleSave,
  onStatusChange,
  onPriorityChange,
  onAssigneeChange,
}: IssueHeaderProps) {
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
    } finally {
      setSaving(false);
    }
  };

  const cancel = () => {
    setDraft(issue.title);
    setEditing(false);
  };

  // PriorityIcon requires a non-null IssuePriority; fall back to "medium" so
  // the picker still renders for issues that have no priority set yet, letting
  // the user assign one via the popover.
  const renderedPriority: IssuePriority = issue.priority ?? "medium";

  return (
    <div className="flex flex-wrap items-center gap-3">
      {issue.identifier && (
        <span
          className="text-xs font-mono shrink-0"
          style={{ color: "var(--mute)" }}
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
          className="font-editorial-serif flex-1 min-w-0 bg-transparent outline-none text-2xl font-medium"
          style={{
            borderBottom: "1px solid var(--edge)",
            color: "var(--ink)",
          }}
          disabled={saving}
        />
      ) : (
        <button
          type="button"
          onClick={() => setEditing(true)}
          className={cn(
            "font-editorial-serif flex-1 min-w-0 text-left text-2xl font-medium rounded px-1 -mx-1 transition-colors",
            saving && "opacity-50",
          )}
          style={{ color: "var(--ink)" }}
          onMouseEnter={(e) => {
            e.currentTarget.style.background = "var(--cream-deeper)";
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.background = "transparent";
          }}
        >
          {issue.title}
        </button>
      )}

      <StatusIcon
        status={issue.status}
        blockerAttention={
          typeof issue.blockerAttention === "object"
            ? issue.blockerAttention
            : null
        }
        onChange={(s) => void onStatusChange(s as IssueStatus)}
        showLabel
      />

      <PriorityIcon
        priority={renderedPriority}
        onChange={(p) => void onPriorityChange(p)}
        showLabel
      />

      <AssigneePicker
        agents={agents}
        selectedAgentId={issue.assigneeAgentId ?? null}
        onChange={(id) => void onAssigneeChange(id)}
        variant="inline"
      />
    </div>
  );
}
