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
}: {
  run: HeartbeatRun;
  agentName: string;
  onDismiss: () => void;
}) {
  return (
    <li className="flex items-center gap-2 px-3 py-2">
      <AlertTriangle className="h-4 w-4 flex-shrink-0" style={{ color: "var(--danger)" }} />
      <div className="flex min-w-0 flex-1 flex-col">
        <span className="truncate text-[var(--ink)]">
          {agentName} — heartbeat run failed
        </span>
        {run.failureReason && (
          <span className="truncate text-xs text-[var(--mute)]">{run.failureReason}</span>
        )}
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
