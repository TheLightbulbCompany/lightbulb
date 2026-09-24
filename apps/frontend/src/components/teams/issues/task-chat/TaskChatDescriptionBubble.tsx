// apps/frontend/src/components/teams/issues/task-chat/TaskChatDescriptionBubble.tsx

// Ported from upstream Paperclip's TaskChatDescriptionBubble
// (paperclip/ui/src/components/task-chat/TaskChatDescriptionBubble.tsx @ v2026.916.1-isol8.2)
// (MIT, (c) 2025 Paperclip AI): the task description as the requester's first
// chat bubble — human-created tasks on the right, agent-created on the left
// under the avatar header; an empty description is a muted ghost row that opens
// the editor, never an empty bubble.
//
// Adaptations:
//   - InlineEditor (mdxeditor)  -> a Textarea; Cmd/Ctrl+Enter or blur saves, Esc cancels
//   - the pencil shows for human-authored briefs too: upstream edits a human
//     description from its properties panel, which ours does not carry
//   - FoldCurtain fold at ~12 lines is not ported (long bodies scroll with the page)

"use client";

import { useEffect, useRef, useState } from "react";
import { Pencil } from "lucide-react";
import { cn } from "@/lib/utils";
import { Textarea } from "@/components/ui/textarea";
import { Identity } from "@/components/teams/shared/components/Identity";
import { TaskChatMarkdown } from "./TaskChatMarkdown";
import { formatTaskChatTimestamp } from "./task-chat-adapter";

export interface TaskChatIssueBrief {
  /** LIVE issue.description (never a frozen copy) — empty string when unset. */
  description: string;
  /** Originating actor: human-created → user bubble, agent-created → agent-side bubble. */
  author: "human" | "agent";
  /** Creating agent's name, shown in the agent-side avatar header. */
  authorName?: string;
  /** issue.createdAt — rendered like the other bubbles' footer timestamps. */
  createdAt?: string | null;
  onSave: (description: string) => void | Promise<unknown>;
}

function DescriptionEditor({ brief, onDone }: { brief: TaskChatIssueBrief; onDone: () => void }) {
  const [draft, setDraft] = useState(brief.description);
  const [saving, setSaving] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.focus();
    el.setSelectionRange(el.value.length, el.value.length);
  }, []);
  const save = async () => {
    const next = draft.trim();
    if (next === brief.description.trim()) return onDone();
    setSaving(true);
    try {
      await brief.onSave(next);
      onDone();
    } finally {
      setSaving(false);
    }
  };
  return (
    <div
      className="w-full rounded-lg px-2 py-1"
      style={{ border: "1px solid var(--edge)", background: "var(--cream-deep)" }}
      data-testid="task-chat-description-editor"
    >
      <Textarea
        ref={ref}
        rows={4}
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
            onDone();
          }
        }}
        style={{ background: "transparent", border: "none", color: "var(--midnight)" }}
      />
    </div>
  );
}

export function TaskChatDescriptionBubble({ brief }: { brief: TaskChatIssueBrief }) {
  const [editing, setEditing] = useState(false);
  const isHuman = brief.author === "human";
  const hasDescription = brief.description.trim().length > 0;

  if (editing) return <DescriptionEditor brief={brief} onDone={() => setEditing(false)} />;

  if (!hasDescription) {
    return (
      <button
        type="button"
        className="flex w-full items-center gap-1.5 rounded-lg px-1 py-1.5 text-left text-sm transition-colors hover:bg-[var(--cream-deep)]"
        style={{ color: "var(--midnight-muted)" }}
        onClick={() => setEditing(true)}
        data-testid="task-chat-description-ghost"
      >
        <Pencil className="h-3.5 w-3.5 shrink-0" aria-hidden />
        Add a description...
      </button>
    );
  }

  const timestamp = formatTaskChatTimestamp(brief.createdAt);
  return (
    <div
      className={cn("group/brief flex w-full flex-col gap-1", isHuman ? "items-end" : "items-start")}
      data-testid="task-chat-description-bubble"
      data-author={brief.author}
    >
      {!isHuman && brief.authorName ? (
        <span className="flex items-center gap-2 px-1">
          <Identity name={brief.authorName} size="sm" avatarOnly />
          <span className="text-sm font-semibold" style={{ color: "var(--midnight)" }}>
            {brief.authorName}
          </span>
        </span>
      ) : null}
      <div className={cn("flex w-full items-start gap-1.5", isHuman ? "flex-row-reverse" : "flex-row")}>
        <div
          className={cn(
            "max-w-[85%] break-words px-3.5 py-2 text-sm",
            isHuman ? "rounded-2xl rounded-br-sm" : "rounded-2xl rounded-bl-sm",
          )}
          style={{
            background: isHuman ? "var(--cream-deep)" : "var(--cream-deeper)",
            border: "1px solid var(--edge)",
            color: "var(--midnight)",
          }}
        >
          <TaskChatMarkdown>{brief.description}</TaskChatMarkdown>
        </div>
        <button
          type="button"
          className="mt-1 shrink-0 rounded-md p-1 opacity-0 transition-opacity hover:bg-[var(--cream-deep)] focus-visible:opacity-100 group-hover/brief:opacity-100"
          style={{ color: "var(--midnight-muted)" }}
          onClick={() => setEditing(true)}
          aria-label="Edit description"
          data-testid="task-chat-description-edit"
        >
          <Pencil className="h-3.5 w-3.5" aria-hidden />
        </button>
      </div>
      {timestamp ? (
        <span className="px-1 text-2xs" style={{ color: "var(--midnight-muted)" }}>
          {timestamp}
        </span>
      ) : null}
    </div>
  );
}
