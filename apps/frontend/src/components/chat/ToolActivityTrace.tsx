"use client";

import * as React from "react";
import { ChevronRight } from "lucide-react";

import { cn } from "@/lib/utils";
import { stripExternalContentMarkers } from "@/lib/externalContentMarkers";
import {
  resolveToolDisplay,
  summarizeToolRun,
  toolRowLabel,
  type ToolDisplay,
} from "@/lib/toolDisplay";

/**
 * One turn's tool activity, as a single readable trace.
 *
 * Replaces the per-call `ToolPill`/`ToolUseIndicator`, which rendered the raw
 * internal tool name in its own `mb-3` block — so a six-step turn scattered six
 * chips down the transcript and told you nothing about what was actually done.
 *
 * The visual model is a margin annotation, not a chip stack: rows hang off one
 * hairline rule, and SUCCESS IS UNCOLOURED. A finished step needs no badge, so
 * the only coloured thing in a healthy turn is nothing at all, and the eye
 * lands on a failure or on the answer. Layout follows the editorial system in
 * globals.css (borders, not surface shifts).
 *
 * Nothing is destroyed, only folded: a settled trace collapses to one summary
 * line, expanding restores every row, and expanding a row still shows the raw
 * args and output. That invariant is what makes aggressive collapsing safe and
 * what lets a production agent be debugged from a screenshot.
 */

export interface ToolCall {
  id: string;
  name: string;
  args: unknown;
  result: unknown;
  status: "running" | "done" | "error";
  /** Live incremental output, shown before the terminal result lands. */
  progressText?: string;
  /** Wall-clock ms. Present on live turns only — history carries no timing. */
  startedAt?: number;
  endedAt?: number;
}

/**
 * A running row with no terminal event after this long stops claiming to be
 * running. We have a known dropped-RPC failure mode; a gerund left spinning on
 * a dead step is the trust-destroying bug of this genre, so the row converts to
 * an explicit unknown-outcome state rather than lying quietly forever.
 */
const STALL_MS = 90_000;

/** Below this, a duration is noise — a 40ms read would jitter the column. */
const DURATION_FLOOR_MS = 2_000;

/** Turns with more calls than this fold when settled. Three or fewer stay open:
 *  folding something that short costs a click to reveal nothing. */
const FOLD_THRESHOLD = 3;

function formatDuration(ms: number): string {
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return seconds ? `${minutes}m ${seconds}s` : `${minutes}m`;
}

/** Renders a tool_result payload as text. Mirrors the old ToolPill's reader. */
function renderToolOutput(content: unknown): string | null {
  if (content == null) return null;
  if (typeof content === "string") return content || null;
  if (!Array.isArray(content)) {
    try {
      return JSON.stringify(content, null, 2);
    } catch {
      return null;
    }
  }
  const parts: string[] = [];
  for (const b of content) {
    if (!b || typeof b !== "object") continue;
    const obj = b as { type?: string; text?: string; omitted?: boolean; bytes?: number };
    if (obj.type === "text" && typeof obj.text === "string") parts.push(obj.text);
    else if (obj.omitted) parts.push(`[${obj.type ?? "blob"} — ${obj.bytes ?? "?"} bytes, omitted]`);
  }
  return parts.join("\n\n") || null;
}

/** True once a running call has gone quiet long enough to stop trusting it. */
function useIsStalled(call: ToolCall): boolean {
  const [now, setNow] = React.useState(() => Date.now());
  const running = call.status === "running";
  const startedAt = call.startedAt;

  React.useEffect(() => {
    if (!running || startedAt === undefined) return;
    // One timer per running row, firing once at the stall boundary — not a
    // ticking interval. Nothing else on the row needs a live clock: durations
    // render from real timestamps at terminal, never counted up in the UI.
    const remaining = startedAt + STALL_MS - Date.now();
    if (remaining <= 0) {
      setNow(Date.now());
      return;
    }
    const t = window.setTimeout(() => setNow(Date.now()), remaining);
    return () => window.clearTimeout(t);
  }, [running, startedAt]);

  return running && startedAt !== undefined && now - startedAt >= STALL_MS;
}

function TraceRow({ call, display }: { call: ToolCall; display: ToolDisplay }) {
  const [open, setOpen] = React.useState(false);
  const stalled = useIsStalled(call);
  const running = call.status === "running" && !stalled;

  const rawOutput = renderToolOutput(call.result) ?? call.progressText ?? null;
  // Untrusted-content sentinels are model-facing plumbing on every surface,
  // including inside an expansion.
  const output = rawOutput === null ? null : stripExternalContentMarkers(rawOutput) || null;
  const hasArgs = !!(
    call.args &&
    typeof call.args === "object" &&
    Object.keys(call.args as Record<string, unknown>).length > 0
  );
  const hasDetails = hasArgs || !!output;

  const durationMs =
    call.startedAt !== undefined && call.endedAt !== undefined
      ? call.endedAt - call.startedAt
      : undefined;
  const showDuration = durationMs !== undefined && durationMs >= DURATION_FLOOR_MS;

  const label = toolRowLabel(display, running);

  return (
    <li className="relative">
      <button
        type="button"
        onClick={() => hasDetails && setOpen((v) => !v)}
        disabled={!hasDetails}
        aria-expanded={hasDetails ? open : undefined}
        // The visible row splits the label across spans for typography. Naming
        // the button gives assistive tech the sentence in one piece — an
        // sr-only duplicate of the same text would just announce it twice.
        aria-label={stalled ? `${label} — outcome unknown` : label}
        className={cn(
          "group flex w-full items-baseline gap-2.5 py-[3px] text-left text-[13px] leading-[1.5]",
          hasDetails ? "cursor-pointer" : "cursor-default",
        )}
      >
        {running && (
          <span
            aria-hidden
            className="absolute -left-[17px] top-[9px] h-[5px] w-[5px] rounded-full bg-[var(--forest-soft)] shadow-[0_0_0_3px_var(--forest-tint)] motion-safe:animate-pulse"
          />
        )}
        {call.status === "error" && (
          <span
            aria-hidden
            className="absolute -left-[14px] bottom-1.5 top-1.5 w-px bg-[var(--danger)]"
          />
        )}
        <span
          className={cn(
            "min-w-0 flex-1 truncate",
            call.status === "error"
              ? "text-[var(--danger)]"
              : running
                ? "text-[var(--ink)]"
                : "text-[var(--ink-soft)]",
          )}
        >
          {/* The verb carries the meaning; the target is secondary. Monospace
              only where the detail IS code — a literal shell command. */}
          <span>{running ? display.gerund : display.past}</span>{" "}
          {display.detail && (
            <span
              className={cn(
                display.kind === "command"
                  ? "font-[family-name:var(--font-mono)] text-[12px]"
                  : undefined,
                call.status === "error" ? "opacity-90" : "text-[var(--mute)]",
              )}
            >
              {display.detail}
            </span>
          )}
          {stalled && <span className="text-[var(--warn)]"> — outcome unknown</span>}
        </span>
        {hasDetails && (
          <ChevronRight
            aria-hidden
            className={cn(
              "h-3 w-3 shrink-0 self-center text-[var(--mute-soft)] opacity-0 transition-[opacity,transform] group-hover:opacity-100 group-focus-visible:opacity-100",
              open && "rotate-90 opacity-100",
            )}
          />
        )}
        {/* Fixed slot so a row gaining a duration never shifts its neighbours.
            Hidden on mobile, where the width is worth more than the number. */}
        <span className="hidden w-[38px] shrink-0 text-right text-[11px] tabular-nums text-[var(--mute-soft)] sm:inline">
          {showDuration ? formatDuration(durationMs!) : ""}
        </span>
      </button>

      {call.status === "error" && output && (
        <div className="mb-1 mt-px border-l-2 border-[var(--danger)] bg-[color-mix(in_srgb,var(--danger)_6%,var(--cream))] px-2.5 py-1.5 font-[family-name:var(--font-mono)] text-[11.5px] leading-[1.5] text-[var(--danger)]">
          {output}
        </div>
      )}

      {open && hasDetails && (
        <div className="mb-1.5 mt-1 max-w-xl space-y-2 rounded-md border border-[var(--edge)] bg-[var(--cream)] p-2 text-xs">
          {hasArgs && (
            <div>
              <div className="mb-0.5 font-medium text-[var(--ink-soft)]">input</div>
              <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded bg-[var(--cream-deeper)] px-2 py-1 text-[var(--ink-soft)]">
                {JSON.stringify(call.args, null, 2)}
              </pre>
            </div>
          )}
          {output && call.status !== "error" && (
            <div>
              <div className="mb-0.5 font-medium text-[var(--ink-soft)]">output</div>
              <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded bg-[var(--cream-deeper)] px-2 py-1 text-[var(--ink-soft)]">
                {output}
              </pre>
            </div>
          )}
        </div>
      )}
    </li>
  );
}

export function ToolActivityTrace({
  calls,
  settled = false,
}: {
  calls: ToolCall[];
  /** The turn has finished. Drives folding and the past-tense summary. */
  settled?: boolean;
}) {
  const displays = React.useMemo(
    () => calls.map((c) => resolveToolDisplay(c.name, c.args)),
    [calls],
  );

  const shouldFold = settled && calls.length > FOLD_THRESHOLD;
  const [userOpened, setUserOpened] = React.useState(false);
  // A turn that folds while the user has it open stays open — the fold is a
  // default, not an override of an explicit choice.
  const folded = shouldFold && !userOpened;

  if (calls.length === 0) return null;

  const totalMs = (() => {
    const starts = calls.map((c) => c.startedAt).filter((v): v is number => v !== undefined);
    const ends = calls.map((c) => c.endedAt).filter((v): v is number => v !== undefined);
    if (!starts.length || !ends.length) return undefined;
    return Math.max(...ends) - Math.min(...starts);
  })();

  if (folded) {
    const summary = summarizeToolRun(
      calls.map((c, i) => ({ display: displays[i]!, failed: c.status === "error" })),
    );
    return (
      <button
        type="button"
        onClick={() => setUserOpened(true)}
        aria-expanded={false}
        className="mb-3.5 mt-0.5 flex items-center gap-1.5 text-left text-[12.5px] text-[var(--mute)] transition-colors hover:text-[var(--ink-soft)]"
      >
        <ChevronRight aria-hidden className="h-3 w-3 shrink-0 text-[var(--mute-soft)]" />
        {summary}
        {totalMs !== undefined && totalMs >= DURATION_FLOOR_MS && (
          <span className="text-[11.5px] tabular-nums text-[var(--mute-soft)]">
            {formatDuration(totalMs)}
          </span>
        )}
      </button>
    );
  }

  return (
    <ul className="mb-4 mt-0.5 list-none border-l border-[var(--edge)] pl-[13px]">
      {calls.map((call, i) => (
        <TraceRow key={call.id} call={call} display={displays[i]!} />
      ))}
    </ul>
  );
}
