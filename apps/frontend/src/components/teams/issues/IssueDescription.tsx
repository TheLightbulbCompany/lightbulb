// apps/frontend/src/components/teams/issues/IssueDescription.tsx

// Ported from upstream Paperclip's issue-body description editor
// (ui/src/pages/IssueDetail.tsx @ c6f5a4d2 on isol8/2026.916-rebase — the
// `<InlineEditor value={issue.description} as="p" multiline
// placeholder="Add a description..." className="text-ui leading-7">` under the
// title; MIT, (c) 2025 Paperclip AI). The description is the issue's body, not
// a thread entry: markdown prose under the title, click to edit.
//
// Adaptations: InlineEditor (mdxeditor) -> a Textarea (Cmd/Ctrl+Enter or blur
// saves, Esc cancels) plus an explicit "Edit description" button, so links in
// the rendered markdown stay clickable; MarkdownBody -> TaskChatMarkdown. Not
// ported: fold curtain, image paste/drop upload, mentions in the editor.

"use client";

import { useEffect, useRef, useState } from "react";
import { Pencil } from "lucide-react";
import { Textarea } from "@/components/ui/textarea";
import { TaskChatMarkdown } from "./task-chat/TaskChatMarkdown";
import { notifyApiError } from "@/lib/notify";

export function IssueDescription({
  description,
  onSave,
}: {
  description: string;
  onSave: (description: string) => Promise<unknown>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(description);
  const [saving, setSaving] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (!editing) setDraft(description);
  }, [description, editing]);

  useEffect(() => {
    const el = ref.current;
    if (!editing || !el) return;
    el.focus();
    el.setSelectionRange(el.value.length, el.value.length);
  }, [editing]);

  const save = async () => {
    const next = draft.trim();
    if (next === description.trim()) return setEditing(false);
    setSaving(true);
    try {
      await onSave(next);
      setEditing(false);
    } catch (err) {
      notifyApiError(err, "Couldn't save the task description");
    } finally {
      setSaving(false);
    }
  };

  if (editing) {
    return (
      <Textarea
        ref={ref}
        rows={6}
        value={draft}
        disabled={saving}
        aria-label="Issue description"
        placeholder="Add a description..."
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => void save()}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            void save();
          } else if (e.key === "Escape") {
            e.preventDefault();
            setDraft(description);
            setEditing(false);
          }
        }}
        className="text-ui leading-7"
        style={{ border: "1px solid var(--edge)", background: "var(--cream)", color: "var(--midnight)" }}
      />
    );
  }

  if (!description.trim()) {
    return (
      <button
        type="button"
        onClick={() => setEditing(true)}
        className="w-full rounded-sm px-1 py-1 text-left text-ui leading-7 transition-colors hover:bg-[var(--cream-deep)]"
        style={{ color: "var(--midnight-muted)" }}
      >
        Add a description...
      </button>
    );
  }

  return (
    <div className="group/description relative" data-testid="issue-description">
      <div className="text-ui leading-7" style={{ color: "var(--midnight)" }}>
        <TaskChatMarkdown>{description}</TaskChatMarkdown>
      </div>
      <button
        type="button"
        onClick={() => setEditing(true)}
        aria-label="Edit description"
        className="absolute right-0 top-0 rounded-md p-1 opacity-0 transition-opacity hover:bg-[var(--cream-deep)] focus-visible:opacity-100 group-hover/description:opacity-100"
        style={{ color: "var(--midnight-muted)" }}
      >
        <Pencil className="h-3.5 w-3.5" aria-hidden />
      </button>
    </div>
  );
}
