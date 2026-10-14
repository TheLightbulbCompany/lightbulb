// apps/frontend/src/components/teams/projects/ProjectDescription.tsx

// Ported from upstream Paperclip's ProjectDetail OverviewContent inline
// description editor (paperclip/ui/src/pages/ProjectDetail.tsx, which uses
// <InlineEditor multiline>) (MIT, © 2025 Paperclip AI). Reskinned onto the
// editorial palette and reduced to a plain inline-editable textarea: click the
// prose to edit, ⌘/Ctrl+Enter or blur to save, Escape to cancel. Drops
// upstream's markdown rendering + image-paste upload.

"use client";

import { useState, useEffect, useRef } from "react";
import { notifyApiError } from "@/lib/notify";

export interface ProjectDescriptionProps {
  description: string;
  onSave: (description: string) => Promise<void>;
}

export function ProjectDescription({
  description,
  onSave,
}: ProjectDescriptionProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(description);
  const [saving, setSaving] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    setDraft(description);
  }, [description]);

  useEffect(() => {
    if (editing && textareaRef.current) {
      textareaRef.current.focus();
      textareaRef.current.select();
    }
  }, [editing]);

  const save = async () => {
    const trimmed = draft.trim();
    if (trimmed === description.trim()) {
      setEditing(false);
      return;
    }
    setSaving(true);
    try {
      await onSave(trimmed);
      setEditing(false);
    } catch (err) {
      notifyApiError(err, "Couldn't save the project description");
    } finally {
      setSaving(false);
    }
  };

  const cancel = () => {
    setDraft(description);
    setEditing(false);
  };

  if (editing) {
    return (
      <textarea
        ref={textareaRef}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => void save()}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            void save();
          } else if (e.key === "Escape") {
            e.preventDefault();
            cancel();
          }
        }}
        rows={4}
        disabled={saving}
        placeholder="Add a description…"
        aria-label="Project description"
        className="w-full resize-y rounded-sm px-3 py-2 text-ui"
        style={{
          border: "1px solid var(--edge)",
          background: "var(--cream)",
          color: "var(--midnight-soft)",
        }}
      />
    );
  }

  return (
    <button
      type="button"
      onClick={() => setEditing(true)}
      className="text-left text-ui whitespace-pre-wrap pl-3 rounded-sm transition-colors"
      style={{
        borderLeft: "2px solid var(--edge)",
        color: description ? "var(--midnight-soft)" : "var(--midnight-muted)",
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.background = "var(--cream-deep)";
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.background = "transparent";
      }}
    >
      {description || "Add a description…"}
    </button>
  );
}
