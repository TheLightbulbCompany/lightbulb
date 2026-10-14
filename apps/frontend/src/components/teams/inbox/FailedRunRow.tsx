// apps/frontend/src/components/teams/inbox/FailedRunRow.tsx
"use client";

import { useState } from "react";
import { notifyApiError } from "@/lib/notify";
import { AlertTriangle } from "lucide-react";
import { EditorialButton } from "@/components/teams/shell";
import { useAgentWake } from "@/components/teams/inbox/hooks/useAgentWake";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { teamsDetailUrl, handleShellLinkClick } from "@/lib/shellNav";
import type { HeartbeatRun } from "@/components/teams/shared/types";

/** Transient state of the Retry action. ``error`` keeps the button live so a
 *  failed wake can be re-attempted; ``done`` is a brief acknowledgement before
 *  the operator dismisses the (still-acked-separately) row. */
type RetryPhase = "idle" | "retrying" | "done" | "error";

export function FailedRunRow({
  run,
  agentName,
  onDismiss,
  count = 1,
}: {
  run: HeartbeatRun;
  agentName: string;
  onDismiss: () => void;
  /** Number of identical occurrences this row collapses. >1 shows a `×N`
   *  badge so a flapping heartbeat doesn't flood the band with N copies. */
  count?: number;
}) {
  const { wakeAgent } = useAgentWake();
  const [retry, setRetry] = useState<RetryPhase>("idle");

  // Title prefers the failure reason (matches the grouping key); fall back
  // to the generic phrase when upstream sent no reason.
  const title = `${agentName} — ${run.failureReason || "heartbeat run failed"}`;

  // Retry wakes the SAME agent that produced the failed run; without an agent
  // id we have nothing to wake, so the control is hidden. A wake failure must
  // not crash the row — fall back to an error phase that lets the operator
  // try again.
  async function onRetry() {
    if (!run.agentId || retry === "retrying") return;
    setRetry("retrying");
    try {
      await wakeAgent(run.agentId, { reason: "retry" });
      setRetry("done");
    } catch (e) {
      // The phase alone says "error" with no why; the toast carries the
      // reason (and, for a trial-gate 402, the upgrade prompt).
      notifyApiError(e, "Couldn't retry the run");
      setRetry("error");
    }
  }

  return (
    <li className="flex items-center gap-2 px-3 py-2">
      <AlertTriangle size={16} className="shrink-0" style={{ color: "var(--err)" }} />
      <div className="flex min-w-0 flex-1 flex-col">
        <span className="flex min-w-0 items-center gap-1.5">
          <span className="truncate text-[var(--midnight)]">{title}</span>
          {count > 1 && (
            <span
              className="shrink-0 rounded-sm px-1.5 py-0.5 text-caption font-medium tabular-nums"
              style={{
                color: "var(--err)",
                background: "var(--cream)",
              }}
              title={`${count} occurrences`}
            >
              ×{count}
            </span>
          )}
        </span>
      </div>
      {run.startedAt && <span className="text-caption text-[var(--midnight-muted)]">{timeAgo(run.startedAt)}</span>}
      <div className="flex items-center gap-2">
        <a
          href={teamsDetailUrl("run", run.id)}
          onClick={(e) => handleShellLinkClick(e, teamsDetailUrl("run", run.id))}
          aria-label={`View run ${run.id}`}
          className="text-caption hover:underline"
          style={{ color: "var(--midnight)" }}
        >
          View
        </a>
        {run.agentId && (
          <EditorialButton
            variant="secondary"
            size="sm"
            onClick={onRetry}
            disabled={retry === "retrying"}
          >
            {retry === "retrying"
              ? "Retrying…"
              : retry === "done"
                ? "Retried"
                : retry === "error"
                  ? "Retry failed"
                  : "Retry"}
          </EditorialButton>
        )}
        <EditorialButton variant="secondary" size="sm" onClick={onDismiss}>
          Dismiss
        </EditorialButton>
      </div>
    </li>
  );
}
