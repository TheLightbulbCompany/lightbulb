// apps/frontend/src/components/teams/issues/task-chat/TaskChatLiveTail.tsx

// Ported from upstream Paperclip @ v2026.916.1-isol8.2 (MIT, (c) 2025 Paperclip AI):
//   - TaskChatLiveRunPill  <- ui/src/components/task-chat/TaskChatLiveRunPill.tsx
//   - TaskChatThinking     <- ui/src/components/task-chat/TaskChatThinking.tsx
//   - TaskChatLiveTail     <- ui/src/components/task-chat/TaskChatLiveTail.tsx
// The live tail of the legacy (non-native-runner) transcript path — which is
// what hermes_gateway is upstream: a "Working for 12s" pill over the streamed
// reply text and provider reasoning.
//
// Adaptations: shimmer-text / --status-agent-running -> animate-pulse + brand
// tokens; useSecondTick -> a local 1s interval; formatDurationWords inlined.
// Tool counts, tool cards, usage readouts, activity groups and protocol cards
// are not ported — hermes_gateway emits none of them.

"use client";

import { useEffect, useState } from "react";
import { Brain, ChevronRight, Loader2 } from "lucide-react";
import { TaskChatMarkdown } from "./TaskChatMarkdown";
import { flattenSelfTalk, isTerminalRunStatus } from "./transcript-adapter";
import type { TaskChatItem, TaskChatThinkingItem } from "./task-chat-model";

/** Upstream lib/issue-chat-messages formatDurationWords (verbatim). */
function formatDurationWords(ms: number | null) {
  if (ms === null || !Number.isFinite(ms) || ms <= 0) return null;
  const totalSeconds = Math.max(1, Math.round(ms / 1000));
  if (totalSeconds < 60) {
    return `${totalSeconds} second${totalSeconds === 1 ? "" : "s"}`;
  }
  const totalMinutes = Math.round(totalSeconds / 60);
  if (totalMinutes < 60) {
    return `${totalMinutes} minute${totalMinutes === 1 ? "" : "s"}`;
  }
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (minutes === 0) {
    return `${hours} hour${hours === 1 ? "" : "s"}`;
  }
  return `${hours} hour${hours === 1 ? "" : "s"} ${minutes} minute${minutes === 1 ? "" : "s"}`;
}

/** Upstream useSecondTick, returning the ticking clock so render stays pure. */
function useSecondTick(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [active]);
  return now;
}

export function TaskChatLiveRunPill({
  status,
  startedAtMs,
  finishedAtMs,
}: {
  status: string;
  /** Run start (startedAt, falling back to createdAt) in ms, or null if unknown. */
  startedAtMs: number | null;
  /** Run finish in ms once terminal; drives the settled elapsed readout. */
  finishedAtMs?: number | null;
}) {
  const active = !isTerminalRunStatus(status);
  const now = useSecondTick(active && startedAtMs != null);
  const elapsedMs =
    startedAtMs == null ? null : (active ? now : finishedAtMs ?? now) - startedAtMs;
  const elapsed = elapsedMs != null ? formatDurationWords(elapsedMs) : null;
  const failed = ["failed", "timed_out", "cancelled", "interrupted"].includes(status);
  const verb = active ? "Working" : failed ? "Stopped" : "Worked";
  const suffix = elapsed ? `for ${elapsed}` : null;

  return (
    <div className="flex min-w-0 items-center gap-2 py-2" data-testid="task-chat-live-run-pill">
      <span className="inline-flex items-center gap-2 text-sm font-medium" style={{ color: "var(--midnight-soft)" }}>
        <span className="flex size-5 shrink-0 items-center justify-center">
          {active ? (
            <Loader2 className="size-4 animate-spin" style={{ color: "var(--midnight-muted)" }} />
          ) : (
            <span
              className="h-1.5 w-1.5 rounded-full"
              style={{ background: failed ? "var(--midnight-faint)" : "var(--ok)" }}
            />
          )}
        </span>
        {active ? <span className="animate-pulse">{verb}</span> : verb}
      </span>
      {suffix ? (
        <span className="text-xs" style={{ color: "var(--midnight-muted)" }}>
          {suffix}
        </span>
      ) : null}
    </div>
  );
}

/** Provider-authored reasoning summaries and deltas; never reconstructed hidden reasoning. */
export function TaskChatThinking({ item }: { item: TaskChatThinkingItem }) {
  const [open, setOpen] = useState(false);
  const active = Boolean(item.streaming);
  const body = item.lines.join("\n").trim();
  const preview = flattenSelfTalk(body);
  const baseLabel = item.channel === "detail" ? "Reasoning detail" : "Reasoning";
  const label = active ? (body ? `${baseLabel}…` : "Thinking…") : item.summaryLabel ?? (body ? baseLabel : "Thought");

  if (!body) {
    return (
      <div
        className="flex min-w-0 items-center gap-2 px-1 py-0.5 text-xs"
        style={{ color: "var(--midnight-muted)" }}
        data-testid="task-chat-thinking"
      >
        <Brain className="h-3.5 w-3.5 shrink-0" aria-hidden />
        <span className={active ? "animate-pulse" : undefined}>{label}</span>
      </div>
    );
  }

  return (
    <div className="flex min-w-0 max-w-full flex-col overflow-hidden text-xs" data-testid="task-chat-thinking">
      <button
        type="button"
        aria-expanded={open}
        aria-label={`${open ? "Collapse" : "Expand"} ${baseLabel.toLowerCase()}: ${preview}`}
        onClick={() => setOpen((v) => !v)}
        className="flex w-full min-w-0 items-center gap-2 overflow-hidden rounded-sm px-1 py-0.5 text-left"
        style={{ color: "var(--midnight-muted)" }}
      >
        <Brain className="h-3.5 w-3.5 shrink-0" aria-hidden />
        {active ? <span className="shrink-0 animate-pulse">{label}</span> : null}
        {!open ? <span className="min-w-0 flex-1 truncate">{preview}</span> : null}
        <ChevronRight className={`h-3 w-3 shrink-0 transition-transform ${open ? "rotate-90" : ""}`} aria-hidden />
      </button>
      {open ? (
        <div
          className="ml-2.5 mt-1 min-w-0 max-w-full overflow-hidden pl-2.5"
          style={{ borderLeft: "2px solid var(--edge)", color: "var(--midnight-muted)" }}
        >
          <TaskChatMarkdown>{body}</TaskChatMarkdown>
        </div>
      ) : null}
    </div>
  );
}

export function TaskChatLiveTail({
  items,
  emptyMessage,
}: {
  items: readonly TaskChatItem[];
  /** Shown when nothing renderable has streamed yet (queued / pre-first-token). */
  emptyMessage?: string;
}) {
  const rows = items
    .map((item) => {
      if (item.kind === "thinking") return <TaskChatThinking key={item.id} item={item} />;
      // Streamed reply text: plain markdown — the settled thread later replaces
      // it with the posted comment bubble, so no author header/bubble chrome here.
      const text = item.text.trim();
      if (!text) return null;
      return (
        <div
          key={item.id}
          className="px-1 text-sm"
          style={{ color: "var(--midnight-soft)" }}
          data-testid="task-chat-live-text"
        >
          <TaskChatMarkdown>{item.text}</TaskChatMarkdown>
        </div>
      );
    })
    .filter(Boolean);

  if (rows.length === 0) {
    return emptyMessage ? (
      <div className="px-1 py-1 text-xs" style={{ color: "var(--midnight-muted)" }}>
        {emptyMessage}
      </div>
    ) : null;
  }
  return <div className="flex flex-col gap-2">{rows}</div>;
}
