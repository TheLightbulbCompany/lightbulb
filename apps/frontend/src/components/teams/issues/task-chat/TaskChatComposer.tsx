// apps/frontend/src/components/teams/issues/task-chat/TaskChatComposer.tsx

// Ported from upstream Paperclip's TaskChatComposer
// (paperclip/ui/src/components/task-chat/TaskChatComposer.tsx @ v2026.916.1-isol8.2)
// (MIT, (c) 2025 Paperclip AI). Kept from upstream: the "Message {agent} —
// describe what you want done…" placeholder, ⌘/Ctrl+Enter to send, the round
// send button that becomes Stop while a run is live and the draft is empty
// (useComposerStop semantics: one stop in flight, error shown inline), and
// @mentions written as `[@Name](agent://id)` (MarkdownEditor's mentionMarkdown
// + shared buildAgentMentionHref), which Paperclip parses to wake that agent.
//
// Adaptations: MarkdownEditor (mdxeditor/lexical, not installed) -> a Textarea
// with a small @-autocomplete over the company's agents. Not ported (part B or
// out of scope): user mentions (members are Clerk-keyed, Paperclip mentions need
// principal ids), attachments/uploads, work modes, reassign, queued edits,
// paused takeover, runner-goal commands, drafts.

"use client";

import { useLayoutEffect, useMemo, useRef, useState } from "react";
import { ArrowUp, Loader2, Square } from "lucide-react";
import { Textarea } from "@/components/ui/textarea";
import { notifyApiError } from "@/lib/notify";
import type { CompanyAgent } from "@/components/teams/shared/types";

/** Upstream MarkdownEditor mentionMarkdown for an agent option. */
export function agentMentionMarkdown(agent: Pick<CompanyAgent, "id" | "name">): string {
  return `[@${agent.name}](agent://${agent.id.trim()}) `;
}

/** The `@query` being typed at the caret, if any. */
function activeMention(text: string, caret: number): { start: number; query: string } | null {
  const upto = text.slice(0, caret);
  const m = /(^|\s)@([\w.-]*)$/.exec(upto);
  return m ? { start: caret - m[2].length - 1, query: m[2].toLowerCase() } : null;
}

export function TaskChatComposer({
  onAdd,
  onStop,
  agents,
  assigneeName,
  disabled = false,
}: {
  onAdd: (body: string) => Promise<void>;
  /** Present while a run is live; the empty-draft send button becomes Stop. */
  onStop?: () => Promise<void>;
  agents: CompanyAgent[];
  assigneeName?: string | null;
  disabled?: boolean;
}) {
  const [body, setBody] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [caret, setCaret] = useState(0);
  const [highlight, setHighlight] = useState(0);
  const [stopping, setStopping] = useState(false);
  const [stopError, setStopError] = useState<string | null>(null);
  const ref = useRef<HTMLTextAreaElement>(null);
  // Caret to restore after a mention insert re-renders the controlled value.
  const pendingCaret = useRef<number | null>(null);
  useLayoutEffect(() => {
    const at = pendingCaret.current;
    if (at === null || !ref.current) return;
    pendingCaret.current = null;
    ref.current.focus();
    ref.current.setSelectionRange(at, at);
  }, [body]);

  const mention = activeMention(body, caret);
  const options = useMemo(
    () =>
      mention
        ? agents.filter((a) => a.name.toLowerCase().includes(mention.query)).slice(0, 6)
        : [],
    [agents, mention],
  );

  const pickMention = (agent: CompanyAgent) => {
    if (!mention) return;
    const insert = agentMentionMarkdown(agent);
    const next = body.slice(0, mention.start) + insert + body.slice(caret);
    const nextCaret = mention.start + insert.length;
    setBody(next);
    setCaret(nextCaret);
    setHighlight(0);
    pendingCaret.current = nextCaret;
  };

  const submit = async () => {
    const trimmed = body.trim();
    if (!trimmed || submitting) return;
    setSubmitting(true);
    try {
      await onAdd(trimmed);
      setBody("");
      setCaret(0);
    } catch (e) {
      notifyApiError(e, "Failed to post comment");
    } finally {
      setSubmitting(false);
    }
  };

  const stop = async () => {
    if (!onStop || stopping) return;
    setStopping(true);
    setStopError(null);
    try {
      await onStop();
    } catch (err) {
      setStopError(err instanceof Error ? err.message : "Unable to stop. Try again.");
    } finally {
      setStopping(false);
    }
  };

  const showStop = !submitting && body.trim().length === 0 && Boolean(onStop || stopping);
  const agentName = assigneeName || "the agent";

  return (
    <div
      className="relative flex flex-col gap-1 rounded-2xl px-3 py-2"
      style={{ border: "1px solid var(--edge)", background: "var(--cream)" }}
      data-testid="task-chat-composer"
    >
      {options.length > 0 ? (
        <ul
          role="listbox"
          aria-label="Mention an agent"
          className="absolute bottom-full left-2 z-20 mb-1 min-w-48 overflow-hidden rounded-md py-1 text-sm shadow-md"
          style={{ background: "var(--cream)", border: "1px solid var(--edge)" }}
        >
          {options.map((agent, i) => (
            <li
              key={agent.id}
              role="option"
              aria-selected={i === highlight}
              className="cursor-pointer px-3 py-1"
              style={{
                background: i === highlight ? "var(--cream-deep)" : undefined,
                color: "var(--midnight)",
              }}
              onMouseDown={(e) => {
                e.preventDefault();
                pickMention(agent);
              }}
            >
              @{agent.name}
            </li>
          ))}
        </ul>
      ) : null}
      <div data-testid="task-chat-composer-input">
        <Textarea
          ref={ref}
          rows={2}
          value={body}
          disabled={disabled || submitting}
          aria-label="Message"
          placeholder={`Message ${agentName} — describe what you want done…`}
          onChange={(e) => {
            setBody(e.target.value);
            setCaret(e.target.selectionStart ?? e.target.value.length);
            setHighlight(0);
          }}
          onSelect={(e) => setCaret(e.currentTarget.selectionStart ?? 0)}
          onKeyDown={(e) => {
            if (options.length > 0) {
              if (e.key === "ArrowDown" || e.key === "ArrowUp") {
                e.preventDefault();
                const step = e.key === "ArrowDown" ? 1 : -1;
                setHighlight((h) => (h + step + options.length) % options.length);
                return;
              }
              if (e.key === "Enter" || e.key === "Tab") {
                e.preventDefault();
                pickMention(options[highlight] ?? options[0]!);
                return;
              }
            }
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              void submit();
            }
          }}
          className="max-h-[28dvh] min-h-12 resize-none border-none px-1 py-1 text-sm shadow-none focus-visible:ring-0"
          style={{ background: "transparent", color: "var(--midnight)" }}
        />
      </div>
      <div className="flex items-center justify-end gap-2" data-testid="task-chat-composer-actions">
        <button
          type="button"
          onClick={() => void (showStop ? stop() : submit())}
          disabled={showStop ? stopping : !body.trim() || submitting || disabled}
          title={showStop ? (stopping ? "Stopping…" : "Stop response") : "Send (⌘+Enter)"}
          aria-label={showStop ? (stopping ? "Stopping…" : "Stop") : "Send"}
          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full transition-transform hover:scale-105 disabled:scale-100 disabled:opacity-50"
          style={{ background: "var(--midnight)", color: "var(--cream)" }}
          data-testid={showStop ? "task-chat-composer-stop" : "task-chat-composer-send"}
        >
          {submitting || (showStop && stopping) ? (
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
          ) : showStop ? (
            <Square className="h-4 w-4 fill-current" aria-hidden />
          ) : (
            <ArrowUp className="h-4 w-4" aria-hidden />
          )}
        </button>
      </div>
      {stopError ? (
        <p role="alert" className="text-xs" style={{ color: "var(--err)" }}>
          {stopError}
        </p>
      ) : null}
    </div>
  );
}
