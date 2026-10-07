// apps/frontend/src/components/teams/projects/ProjectHeader.tsx

// Ported from upstream Paperclip's pages/ProjectDetail.tsx header section
// (paperclip/ui/src/pages/ProjectDetail.tsx) (MIT, © 2025 Paperclip AI).
// Mirrors the issue/goal inline-editable-title pattern (IssueHeader.tsx /
// GoalHeader.tsx): a "Project" overline + an inline-editable name. Drops the
// upstream icon/color tile picker, plugin slots, and membership affordances.

"use client";

import { useState, useEffect, useRef } from "react";
import { cn } from "@/lib/utils";
import type { Project } from "@/components/teams/shared/types";

export interface ProjectHeaderProps {
  project: Project;
  onNameSave: (name: string) => Promise<void>;
}

export function ProjectHeader({ project, onNameSave }: ProjectHeaderProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(project.name);
  const [saving, setSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setDraft(project.name);
  }, [project.name]);

  useEffect(() => {
    if (editing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [editing]);

  const save = async () => {
    const trimmed = draft.trim();
    if (!trimmed || trimmed === project.name) {
      setDraft(project.name);
      setEditing(false);
      return;
    }
    setSaving(true);
    try {
      await onNameSave(trimmed);
      setEditing(false);
    } finally {
      setSaving(false);
    }
  };

  const cancel = () => {
    setDraft(project.name);
    setEditing(false);
  };

  return (
    <div className="flex flex-col gap-2">
      <span
        className="label-caps font-mono"
        style={{ color: "var(--midnight-muted)" }}
      >
        Project
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
            {project.name}
          </button>
        )}
      </div>
    </div>
  );
}
