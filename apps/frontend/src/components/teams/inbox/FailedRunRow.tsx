// apps/frontend/src/components/teams/inbox/FailedRunRow.tsx
"use client";

import { AlertTriangle } from "lucide-react";
import { EditorialButton } from "@/components/teams/shell";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import type { HeartbeatRun } from "@/components/teams/shared/types";

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
  // Title prefers the failure reason (matches the grouping key); fall back
  // to the generic phrase when upstream sent no reason.
  const title = `${agentName} — ${run.failureReason || "heartbeat run failed"}`;
  return (
    <li className="flex items-center gap-2 px-3 py-2">
      <AlertTriangle className="h-4 w-4 flex-shrink-0" style={{ color: "var(--danger)" }} />
      <div className="flex min-w-0 flex-1 flex-col">
        <span className="flex min-w-0 items-center gap-1.5">
          <span className="truncate text-[var(--ink)]">{title}</span>
          {count > 1 && (
            <span
              className="flex-shrink-0 rounded px-1.5 py-0.5 text-[11px] font-medium tabular-nums"
              style={{
                color: "var(--danger)",
                background: "color-mix(in srgb, var(--danger) 12%, transparent)",
              }}
              title={`${count} occurrences`}
            >
              ×{count}
            </span>
          )}
        </span>
      </div>
      {run.startedAt && <span className="text-xs text-[var(--mute)]">{timeAgo(run.startedAt)}</span>}
      <div className="flex gap-2">
        {run.agentId && (
          <a
            href={`/teams/agents/${run.agentId}/runs/${run.id}`}
            className="text-sm hover:underline"
            style={{ color: "var(--forest)" }}
          >
            View
          </a>
        )}
        <EditorialButton variant="secondary" size="sm" onClick={onDismiss}>
          Dismiss
        </EditorialButton>
      </div>
    </li>
  );
}
