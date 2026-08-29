// apps/frontend/src/components/teams/goals/GoalHeader.tsx

// Ported from upstream Paperclip's pages/GoalDetail.tsx header section
// (paperclip/ui/src/pages/GoalDetail.tsx) (MIT, © 2025 Paperclip AI).
// Mirrors the issue IssueHeader.tsx inline-editable-title pattern. Shows the
// goal level overline + an inline-editable title + the status picker.

"use client";

import { useState, useEffect, useRef } from "react";
import { cn } from "@/lib/utils";
import type { Goal, GoalStatus } from "@/components/teams/shared/types";
import { GoalStatusIcon } from "./GoalStatusIcon";

export interface GoalHeaderProps {
  goal: Goal;
  onTitleSave: (title: string) => Promise<void>;
  onStatusChange: (status: GoalStatus) => Promise<void>;
}

export function GoalHeader({ goal, onTitleSave, onStatusChange }: GoalHeaderProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(goal.title);
  const [saving, setSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setDraft(goal.title);
  }, [goal.title]);

  useEffect(() => {
    if (editing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [editing]);

  const save = async () => {
    const trimmed = draft.trim();
    if (!trimmed || trimmed === goal.title) {
      setDraft(goal.title);
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
    setDraft(goal.title);
    setEditing(false);
  };

  return (
    <div className="flex flex-col gap-2">
      <span
        className="label-caps font-mono"
        style={{ color: "var(--midnight-muted)" }}
      >
        {goal.level} goal
      </span>
      <div className="flex flex-wrap items-center gap-3">
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
            {goal.title}
          </button>
        )}

        <GoalStatusIcon
          status={goal.status}
          onChange={(s) => void onStatusChange(s)}
          showLabel
        />
      </div>
    </div>
  );
}
