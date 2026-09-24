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
//
// Part B2: the pending-input takeover (upstream `takeover` / `pendingTakeover`
// props) — a pending interaction replaces the input with its form under a
// shared top row (label or portalled title, "N pending", pagination slot,
// dismiss X), Skip inline or below, and the "N pending" indicator above the
// input once dismissed.

"use client";

import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ArrowUp, CircleHelp, Loader2, Square, X } from "lucide-react";
import { EditorialButton } from "@/components/teams/shell";
import { TaskChatComposerTakeoverActionsContext } from "./TaskChatComposerTakeoverContext";
import { Textarea } from "@/components/ui/textarea";
import { notifyApiError } from "@/lib/notify";
import type { CompanyAgent } from "@/components/teams/shared/types";

/** Upstream TaskChatComposerTakeover. */
export interface TaskChatComposerTakeover {
  id: string;
  label: string;
  pendingCount: number;
  content: ReactNode;
  /** Hides the takeover without resolving it; the pending indicator can reopen it. */
  onDismiss: () => void;
  onSkip: () => Promise<void> | void;
  onShowNext?: () => void;
  /** Places Skip inside a structured form's action row. */
  inlineSkip?: boolean;
  /** Some decision surfaces already provide a non-accept path of their own. */
  hideSkip?: boolean;
}

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
  takeover = null,
  pendingTakeover = null,
}: {
  takeover?: TaskChatComposerTakeover | null;
  pendingTakeover?: { count: number; label?: string; onOpen: () => void } | null;
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
  // -1 = nothing highlighted: a bare `@` shows the list without claiming Enter/Tab.
  const [highlight, setHighlight] = useState(-1);
  // Popup dismissed with Esc for the mention that starts at this offset.
  const [dismissedAt, setDismissedAt] = useState<number | null>(null);
  const listboxId = useId();
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
      mention && mention.start !== dismissedAt
        ? agents.filter((a) => a.name.toLowerCase().includes(mention.query)).slice(0, 6)
        : [],
    [agents, mention, dismissedAt],
  );

  const pickMention = (agent: CompanyAgent) => {
    if (!mention) return;
    const insert = agentMentionMarkdown(agent);
    const next = body.slice(0, mention.start) + insert + body.slice(caret);
    const nextCaret = mention.start + insert.length;
    setBody(next);
    setCaret(nextCaret);
    setHighlight(-1);
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

  const [takeoverBusy, setTakeoverBusy] = useState(false);
  const [takeoverError, setTakeoverError] = useState<string | null>(null);
  const [takeoverHeaderClaimed, setTakeoverHeaderClaimed] = useState(false);
  const [takeoverHeaderSlot, setTakeoverHeaderSlot] = useState<HTMLElement | null>(null);
  const [takeoverControlsSlot, setTakeoverControlsSlot] = useState<HTMLElement | null>(null);
  useEffect(() => {
    setTakeoverBusy(false);
    setTakeoverError(null);
  }, [takeover?.id]);

  function skipTakeover() {
    if (!takeover) return;
    setTakeoverBusy(true);
    setTakeoverError(null);
    Promise.resolve(takeover.onSkip()).catch((cause) => {
      setTakeoverBusy(false);
      setTakeoverError(cause instanceof Error ? cause.message : "This request could not be skipped.");
    });
  }

  const takeoverSkipButton = takeover ? (
    <EditorialButton type="button" size="sm" variant="secondary" disabled={takeoverBusy} onClick={skipTakeover}>
      {takeoverBusy ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : null}
      Skip
    </EditorialButton>
  ) : null;

  if (takeover && !submitting) {
    return (
      <section
        className="relative rounded-2xl px-3 py-3"
        style={{ border: "1px solid var(--edge)", background: "var(--cream)" }}
        aria-label={takeover.label}
        data-testid="task-chat-composer-takeover"
      >
        <div className="mb-3 flex min-w-0 items-center gap-2" data-testid="task-chat-composer-takeover-header">
          <div className="min-w-0 flex-1">
            {!takeoverHeaderClaimed ? (
              <strong className="block truncate text-sm font-medium" style={{ color: "var(--midnight)" }}>
                {takeover.label}
              </strong>
            ) : null}
            <div ref={setTakeoverHeaderSlot} className="flex min-w-0 items-center" />
          </div>
          <div className="ml-auto flex shrink-0 items-center gap-1">
            {takeover.pendingCount > 1 ? (
              <EditorialButton type="button" size="sm" variant="ghost" onClick={takeover.onShowNext}>
                {takeover.pendingCount} pending
              </EditorialButton>
            ) : null}
            <div ref={setTakeoverControlsSlot} className="flex shrink-0 items-center" />
            <EditorialButton
              type="button"
              size="icon"
              variant="ghost"
              aria-label={`Dismiss ${takeover.label}`}
              disabled={takeoverBusy}
              onClick={takeover.onDismiss}
            >
              <X aria-hidden className="h-4 w-4" />
            </EditorialButton>
          </div>
        </div>
        <div className="pr-1" data-testid="task-chat-composer-takeover-body">
          <TaskChatComposerTakeoverActionsContext.Provider
            value={{
              skipButton: takeover.inlineSkip && !takeover.hideSkip ? takeoverSkipButton : null,
              dismiss: takeover.onDismiss,
              headerSlot: takeoverHeaderSlot,
              controlsSlot: takeoverControlsSlot,
              setHeaderClaimed: setTakeoverHeaderClaimed,
            }}
          >
            {takeover.content}
          </TaskChatComposerTakeoverActionsContext.Provider>
        </div>
        {takeoverError ? (
          <p className="mt-2 text-sm" role="alert" style={{ color: "var(--err)" }}>
            {takeoverError}
          </p>
        ) : null}
        {!takeover.inlineSkip && !takeover.hideSkip ? (
          <div className="mt-3 flex items-center justify-end gap-2">{takeoverSkipButton}</div>
        ) : null}
      </section>
    );
  }

  const showStop = !submitting && body.trim().length === 0 && Boolean(onStop || stopping);
  const agentName = assigneeName || "the agent";

  return (
    <div
      className="relative flex flex-col gap-1 rounded-2xl px-3 py-2"
      style={{ border: "1px solid var(--edge)", background: "var(--cream)" }}
      data-testid="task-chat-composer"
    >
      {pendingTakeover ? (
        <button
          type="button"
          className="mb-1 flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left text-xs transition-colors hover:bg-[var(--cream-deeper)]"
          style={{ background: "var(--cream-deep)", color: "var(--midnight-muted)" }}
          onClick={pendingTakeover.onOpen}
          data-testid="task-chat-pending-input-indicator"
        >
          <CircleHelp className="h-4 w-4 shrink-0" aria-hidden />
          <span className="min-w-0 flex-1 truncate">{pendingTakeover.label ?? "Pending input"}</span>
          <span className="shrink-0 font-medium">{pendingTakeover.count} pending</span>
        </button>
      ) : null}
      {options.length > 0 ? (
        <ul
          id={listboxId}
          role="listbox"
          aria-label="Mention an agent"
          className="absolute bottom-full left-2 z-20 mb-1 min-w-48 overflow-hidden rounded-md py-1 text-sm shadow-md"
          style={{ background: "var(--cream)", border: "1px solid var(--edge)" }}
        >
          {options.map((agent, i) => (
            <li
              key={agent.id}
              id={`${listboxId}-${i}`}
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
          role="combobox"
          aria-autocomplete="list"
          aria-expanded={options.length > 0}
          aria-controls={options.length > 0 ? listboxId : undefined}
          aria-activedescendant={
            options.length > 0 && highlight >= 0 ? `${listboxId}-${highlight}` : undefined
          }
          placeholder={`Message ${agentName} — describe what you want done…`}
          onChange={(e) => {
            setBody(e.target.value);
            const nextCaret = e.target.selectionStart ?? e.target.value.length;
            setCaret(nextCaret);
            // A typed query pre-highlights the first match; a bare `@` does not.
            setHighlight(activeMention(e.target.value, nextCaret)?.query ? 0 : -1);
          }}
          onSelect={(e) => setCaret(e.currentTarget.selectionStart ?? 0)}
          onKeyDown={(e) => {
            // Send always wins, even with the mention list open.
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              void submit();
              return;
            }
            if (options.length === 0) return;
            if (e.key === "Escape") {
              e.preventDefault();
              setDismissedAt(mention?.start ?? null);
              setHighlight(-1);
              return;
            }
            if (e.key === "ArrowDown" || e.key === "ArrowUp") {
              e.preventDefault();
              const step = e.key === "ArrowDown" ? 1 : -1;
              setHighlight((h) => (h < 0 ? (step > 0 ? 0 : options.length - 1) : (h + step + options.length) % options.length));
              return;
            }
            // Enter/Tab pick only an actively highlighted option; otherwise Tab
            // moves focus on and Enter inserts a newline as usual.
            if ((e.key === "Enter" || e.key === "Tab") && highlight >= 0 && options[highlight]) {
              e.preventDefault();
              pickMention(options[highlight]);
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
