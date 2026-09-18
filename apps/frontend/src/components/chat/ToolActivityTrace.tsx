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
 * Nothing is destroyed, only folded: any multi-step trace — running or settled
 * — collapses to one summary line, expanding restores every row, and expanding
 * a row still shows the raw args and output. That invariant is what makes
 * aggressive collapsing safe and what lets a production agent be debugged from
 * a screenshot.
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
  /** "activity" for a non-tool activity pill (plan/patch/item/acp) — these
   *  already carry a human sentence in `name` and must bypass the tool-name
   *  resolver below rather than being re-labeled from it. Absent/"tool" for
   *  a real tool call. */
  kind?: "tool" | "activity";
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

/**
 * Turns with more calls than this fold — running or settled. One is the floor,
 * not a taste knob: a lone call is ALREADY one quiet line, and its row says
 * more than the summary would ("Read src/app.tsx" vs "Read 1 file"), so folding
 * it costs a click to reveal less. Everything above that collapses to a single
 * line by default, which is the whole point of this component.
 */
const FOLD_THRESHOLD = 1;

function formatDuration(ms: number): string {
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return seconds ? `${minutes}m ${seconds}s` : `${minutes}m`;
}

/**
 * Display for a non-tool activity pill. `resolveToolDisplay` looks up its
 * config by TOOL NAME, and an activity's `name` is already a human sentence
 * from the backend ("Applied a patch to 3 files") — running it through that
 * resolver Title-Cases the sentence and can append an unrelated raw id from
 * `CONFIG.fallback.detailKeys`. The old pill printed the name verbatim; this
 * does the same by never calling the resolver at all.
 */
function activityDisplay(name: string): ToolDisplay {
  return { title: name, gerund: name, past: name, kind: "generic" };
}

/**
 * The folded line for a turn that is STILL RUNNING.
 *
 * A settled turn summarises what happened ("Read 6 files · ran 3 commands");
 * mid-run that phrasing reads as a finished report of a thing that is still
 * moving. So the live line is the step happening RIGHT NOW, in the present
 * tense — the same sentence its row would carry — with failures so far kept
 * visible, since folding must never be the reason a user misses one.
 */
function summarizeLiveToolRun(calls: ToolCall[], displays: ToolDisplay[]): string {
  let current = -1;
  for (let i = calls.length - 1; i >= 0; i -= 1) {
    if (calls[i]!.status === "running") {
      current = i;
      break;
    }
  }
  const base =
    current >= 0
      ? toolRowLabel(displays[current]!, true)
      : summarizeToolRun(calls.map((c, i) => ({ display: displays[i]!, failed: c.status === "error" })));
  const failures = calls.filter((c) => c.status === "error").length;
  return failures > 0 ? `${base} · ${failures} failed` : base;
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
          "group flex w-full items-baseline gap-2.5 py-[3px] text-left text-ui leading-snug",
          hasDetails ? "cursor-pointer" : "cursor-default",
        )}
      >
        {running && (
          <span
            aria-hidden
            className="absolute -left-[17px] top-[9px] h-[5px] w-[5px] rounded-full bg-[var(--brass)] shadow-[0_0_0_3px_var(--cream-deep)] motion-safe:animate-pulse"
          />
        )}
        {call.status === "error" && (
          <span
            aria-hidden
            className="absolute -left-[14px] bottom-1.5 top-1.5 w-px bg-[var(--err)]"
          />
        )}
        <span
          className={cn(
            "min-w-0 flex-1 truncate",
            call.status === "error"
              ? "text-[var(--err)]"
              : running
                ? "text-[var(--midnight)]"
                : "text-[var(--midnight-soft)]",
          )}
        >
          {/* The verb carries the meaning; the target is secondary. Monospace
              only where the detail IS code — a literal shell command. */}
          <span>{running ? display.gerund : display.past}</span>{" "}
          {display.detail && (
            <span
              className={cn(
                display.kind === "command"
                  ? "font-[family-name:var(--font-mono)] text-xs"
                  : undefined,
                call.status === "error" ? "opacity-90" : "text-[var(--midnight-muted)]",
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
              "h-3 w-3 shrink-0 self-center text-[var(--midnight-faint)] opacity-0 transition-[opacity,transform] group-hover:opacity-100 group-focus-visible:opacity-100",
              open && "rotate-90 opacity-100",
            )}
          />
        )}
        {/* Fixed slot so a row gaining a duration never shifts its neighbours.
            Hidden on mobile, where the width is worth more than the number. */}
        <span className="hidden w-[38px] shrink-0 text-right text-caption tabular-nums text-[var(--midnight-muted)] sm:inline">
          {showDuration ? formatDuration(durationMs!) : ""}
        </span>
      </button>

      {call.status === "error" && output && (
        <div className="mb-1 mt-px max-h-48 overflow-auto whitespace-pre-wrap break-words border-l-2 border-[var(--err)] bg-[color-mix(in_srgb,var(--err)_6%,var(--cream))] px-2.5 py-1.5 font-[family-name:var(--font-mono)] text-caption leading-snug text-[var(--err)]">
          {output}
        </div>
      )}

      {open && hasDetails && (
        <div className="mb-1.5 mt-1 max-w-xl space-y-2 rounded-md border border-[var(--edge)] bg-[var(--cream)] p-2 text-xs">
          {hasArgs && (
            <div>
              <div className="mb-0.5 font-medium text-[var(--midnight-soft)]">input</div>
              <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-sm bg-[var(--cream-deep)] px-2 py-1 text-[var(--midnight-soft)]">
                {JSON.stringify(call.args, null, 2)}
              </pre>
            </div>
          )}
          {output && call.status !== "error" && (
            <div>
              <div className="mb-0.5 font-medium text-[var(--midnight-soft)]">output</div>
              <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-sm bg-[var(--cream-deep)] px-2 py-1 text-[var(--midnight-soft)]">
                {output}
              </pre>
            </div>
          )}
        </div>
      )}
    </li>
  );
}

export function ToolActivityTrace({ calls }: { calls: ToolCall[] }) {
  // `calls` is a fresh array on every parent render at both call sites (a
  // literal in MessageList's flushTrace, coalesceToolRuns' output), so a
  // referential memo here never hits — just map plainly.
  const displays = calls.map((c) =>
    c.kind === "activity" ? activityDisplay(c.name) : resolveToolDisplay(c.name, c.args),
  );

  // A trace is settled once every call in it has reached a terminal status.
  // Both call sites used to compute this identically and pass it down as a
  // `settled` prop — derived here instead so there's one place to get it
  // right (and vacuously true for an empty list, harmless since that also
  // short-circuits to `null` below).
  const settled = calls.every((c) => c.status !== "running");
  // Folding no longer waits for the turn to settle. A running turn is exactly
  // when the trace is at its ugliest — rows arrive one by one, the block grows,
  // then collapses under the reader — so it folds from the first extra call and
  // the one line it leaves behind narrates the live step instead.
  const shouldFold = calls.length > FOLD_THRESHOLD;
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
    const summary = settled
      ? summarizeToolRun(
          calls.map((c, i) => ({ display: displays[i]!, failed: c.status === "error" })),
        )
      : summarizeLiveToolRun(calls, displays);
    // The trailing slot is one number, never two: a finished turn is worth a
    // duration, a live one is worth knowing how much is hiding behind the line.
    const meta = settled
      ? totalMs !== undefined && totalMs >= DURATION_FLOOR_MS
        ? formatDuration(totalMs)
        : null
      : `${calls.length} steps`;
    return (
      <button
        type="button"
        onClick={() => setUserOpened(true)}
        aria-expanded={false}
        className="mb-3.5 mt-0.5 flex items-center gap-1.5 text-left text-ui text-[var(--midnight-muted)] transition-colors hover:text-[var(--midnight-soft)]"
      >
        {!settled && (
          // Same brass pulse a running row wears, so a folded live turn still
          // reads as motion rather than a finished sentence.
          <span
            aria-hidden
            className="h-[5px] w-[5px] shrink-0 rounded-full bg-[var(--brass)] motion-safe:animate-pulse"
          />
        )}
        <ChevronRight aria-hidden className="h-3 w-3 shrink-0 text-[var(--midnight-faint)]" />
        {summary}
        {meta && <span className="text-caption tabular-nums text-[var(--midnight-muted)]">{meta}</span>}
      </button>
    );
  }

  return (
    <>
      {/* Folding is a default, not a one-way trip: a trace over the fold
          threshold that the user opened (userOpened) can still be sent back to
          its one-line summary. Only shown when it would actually re-fold
          (shouldFold) — a lone call was never folded, so there's nothing to
          collapse it back to. Sits OUTSIDE the `<ul>` below — it's a
          trace-level control, not a row. */}
      {shouldFold && (
        <button
          type="button"
          onClick={() => setUserOpened(false)}
          aria-expanded={true}
          className="mb-1 flex items-center gap-1.5 text-left text-ui text-[var(--midnight-muted)] transition-colors hover:text-[var(--midnight-soft)]"
        >
          <ChevronRight aria-hidden className="h-3 w-3 shrink-0 rotate-90 text-[var(--midnight-faint)]" />
          Collapse
        </button>
      )}
      <ul className="mb-4 mt-0.5 list-none border-l border-[var(--edge)] pl-[13px]">
        {calls.map((call, i) => (
          <TraceRow key={call.id} call={call} display={displays[i]!} />
        ))}
      </ul>
    </>
  );
}
