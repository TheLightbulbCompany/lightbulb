// apps/frontend/src/components/teams/dashboard/ActiveAgentsPanel.tsx
//
// Ported from upstream Paperclip's ActiveAgentsPanel
// (paperclip/ui/src/components/ActiveAgentsPanel.tsx) (MIT, (c) 2025
// Paperclip AI). Same run-card grid + live-status semantics; editorial
// reskin onto OUR cream/forest theme (tokens --cream/--ink/--mute/--forest/
// --edge, the `editorial-card` class, `font-editorial-serif` heading).
//
// LIVE TRANSCRIPT (v1): active run cards now show a polled tail of the run's
//   raw log — the "Working… / Executing command / …" activity. Backed by the
//   `useRunLog` hook → BFF `GET /teams/heartbeat-runs/{id}/log` → upstream
//   `GET /api/heartbeat-runs/{id}/log`. POLLED (15s), last-N-lines only; NOT
//   the upstream RunChatSurface/WebSocket streaming machinery. Gated to
//   active runs so finished cards don't poll, and degrades to the "Working…"
//   placeholder on error/empty.
//
// DELIBERATE SCOPE CUTS vs upstream:
//   * No realtime (WebSocket) overlay or incremental byte-offset walking —
//     each poll re-reads a bounded tail. Streaming is a deliberate follow-up.
//   * No adapter/transcript-builder — we surface the plain `chunk` text.
//   * No recovery chip (upstream's IssueRecoveryAction surface) — out of
//     scope for v1.

import Link from "next/link";
import { ExternalLink } from "lucide-react";
import { Identity } from "@/components/teams/shared/components/Identity";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { cn } from "@/lib/utils";
import type { Issue } from "@/components/teams/shared/types";
import {
  isRunActive,
  type LiveRun,
} from "@/components/teams/dashboard/hooks/useLiveRuns";
import { useRunLog } from "@/components/teams/dashboard/hooks/useRunLog";

// Cap how many cards render in the dashboard strip so the "Agents" section
// stays a glanceable header band, not an unbounded grid. Upstream used 4.
const DASHBOARD_RUN_CARD_LIMIT = 4;

/* ---------- live-status line ---------- */

function liveStatusLabel(run: LiveRun): string {
  if (isRunActive(run)) return "Live now";
  if (run.finishedAt) return `Finished ${timeAgo(run.finishedAt)}`;
  const started = run.startedAt ?? run.createdAt;
  if (started) return `Started ${timeAgo(started)}`;
  return "Idle";
}

/* ---------- live activity area ---------- */

/**
 * The card's activity area. For ACTIVE runs it polls the run log
 * (`useRunLog`, gated on `active` so finished cards never poll) and renders a
 * compact, scrollable, monospace transcript tail — the live "Working… /
 * Executing command / …" activity. It degrades gracefully:
 *
 *   live transcript lines → monospace transcript well
 *   else `nextAction` hint → quiet single-line hint
 *   else active           → "Working…" placeholder
 *   else                  → "No activity captured."
 *
 * `useRunLog` is always called (rules of hooks); only the fetch is gated, so
 * an inactive card contributes zero BFF traffic and still renders its
 * placeholder.
 */
function RunActivity({
  runId,
  active,
  activityLine,
}: {
  runId: string;
  active: boolean;
  activityLine: string | null;
}) {
  const { lines } = useRunLog(runId, active);

  if (lines.length > 0) {
    return (
      <div
        className="max-h-28 overflow-y-auto rounded-lg px-2.5 py-2 font-mono text-[10px] leading-relaxed"
        style={{
          background: "var(--cream-deepest)",
          color: "var(--ink-soft)",
          borderTop: "1px solid var(--edge)",
        }}
        aria-label="Live agent activity"
      >
        {lines.map((line, i) => (
          <div key={i} className="whitespace-pre-wrap break-words">
            {line}
          </div>
        ))}
      </div>
    );
  }

  // No transcript yet (loading/empty/inactive) — fall back to the quiet
  // placeholder line so the card body keeps a consistent height.
  return (
    <div
      className={cn(
        "rounded-lg px-2.5 py-2 text-xs",
        !activityLine && "italic",
      )}
      style={{
        background: "var(--cream-deepest)",
        color: "var(--mute)",
        borderTop: "1px solid var(--edge)",
      }}
    >
      {activityLine ?? (active ? "Working…" : "No activity captured.")}
    </div>
  );
}

/* ---------- run card ---------- */

function AgentRunCard({
  run,
  issue,
}: {
  run: LiveRun;
  issue?: Issue;
}) {
  const active = isRunActive(run);
  const agentName = run.agentName?.trim() || "Agent";
  // Linked-task label: prefer the human identifier + title; fall back to a
  // short id slug so the chip still links somewhere meaningful.
  const issueKey =
    issue?.identifier ?? (run.issueId ? run.issueId.slice(0, 8) : null);
  const issueTitle = issue?.title ?? null;
  // The only "activity" signal the live-run row carries is `nextAction` (a
  // short upstream-stamped hint). Surface it in place of a transcript so the
  // card isn't a bare status pill. Empty for most rows — handled gracefully.
  const activityLine = run.nextAction?.trim() || null;

  return (
    <div
      className="editorial-card flex flex-col gap-3 overflow-hidden p-4"
      style={
        active
          ? {
              borderColor: "color-mix(in srgb, var(--forest) 35%, var(--edge))",
              background:
                "color-mix(in srgb, var(--forest-tint) 55%, var(--cream))",
            }
          : undefined
      }
    >
      {/* Header: live dot + agent identity, external link top-right */}
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          {active ? (
            <span className="relative flex size-2.5 shrink-0" aria-hidden>
              <span
                className="absolute inline-flex size-full animate-ping rounded-full opacity-60"
                style={{ backgroundColor: "var(--forest-soft)" }}
              />
              <span
                className="relative inline-flex size-2.5 rounded-full"
                style={{ backgroundColor: "var(--forest)" }}
              />
            </span>
          ) : (
            <span
              className="inline-flex size-2.5 shrink-0 rounded-full"
              style={{ backgroundColor: "var(--mute)" }}
              aria-hidden
            />
          )}
          <Identity name={agentName} size="sm" />
        </div>

        {run.agentId && (
          <Link
            href={`/teams/agents/${run.agentId}/runs/${run.id}`}
            className="inline-flex shrink-0 items-center rounded-full border p-1 transition-colors hover:opacity-80"
            style={{ borderColor: "var(--edge)", color: "var(--mute)" }}
            aria-label="Open run"
            title="Open run"
          >
            <ExternalLink className="size-3" />
          </Link>
        )}
      </div>

      {/* Live status line */}
      <div className="text-[11px]" style={{ color: "var(--mute)" }}>
        {liveStatusLabel(run)}
      </div>

      {/* Linked task */}
      {run.issueId && issueKey && (
        <Link
          href={`/teams/issues/${run.issueId}`}
          className="block rounded-lg border px-2.5 py-2 text-xs transition-colors hover:opacity-80"
          style={{
            borderColor: "var(--edge)",
            background: "var(--cream-deeper)",
            color: active ? "var(--forest)" : "var(--ink)",
          }}
          title={issueTitle ? `${issueKey} — ${issueTitle}` : issueKey}
        >
          <span className="line-clamp-2">
            <span className="font-medium">{issueKey}</span>
            {issueTitle ? <span> — {issueTitle}</span> : null}
          </span>
        </Link>
      )}

      {/* Compact activity area — live transcript tail for active runs,
          graceful nextAction / "Working…" / "No activity captured."
          fallback otherwise. See RunActivity. */}
      <RunActivity runId={run.id} active={active} activityLine={activityLine} />
    </div>
  );
}

/* ---------- panel ---------- */

export function ActiveAgentsPanel({
  runs,
  issuesById,
  cardLimit = DASHBOARD_RUN_CARD_LIMIT,
}: {
  runs: LiveRun[];
  /** Lookup of issue id → Issue, sourced from the dashboard's `/issues` read
   *  so we don't fan out a per-issue fetch from the card. Missing entries
   *  degrade to an id-slug chip. */
  issuesById: Map<string, Issue>;
  cardLimit?: number;
}) {
  const visibleRuns = runs.slice(0, cardLimit);

  return (
    <section aria-label="Active agents" className="min-w-0">
      <h2
        className="mb-3 font-editorial-serif text-2xl"
        style={{ color: "var(--ink)" }}
      >
        Agents
      </h2>

      {visibleRuns.length === 0 ? (
        <div
          className="editorial-card px-4 py-3 font-editorial-serif text-base italic"
          style={{ color: "var(--mute)" }}
        >
          No active agent runs.
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 sm:gap-4 xl:grid-cols-4">
          {visibleRuns.map((run) => (
            <AgentRunCard
              key={run.id}
              run={run}
              issue={run.issueId ? issuesById.get(run.issueId) : undefined}
            />
          ))}
        </div>
      )}
    </section>
  );
}
