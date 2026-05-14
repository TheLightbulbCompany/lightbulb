"use client";

import { useState } from "react";
import { X, ChevronDown, ChevronRight, Play, Pencil, Copy } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useGatewayRpc } from "@/hooks/useGatewayRpc";
import { RunTranscript, firstUserMessage } from "./RunTranscript";
import { RunMetadata } from "./RunMetadata";
import { projectSessionMessages } from "./sessionMessageAdapter";
import { formatAbsoluteTime, formatDuration } from "./formatters";
import type { CronJob, CronRunEntry } from "./types";

const STATUS_PILL: Record<string, string> = {
  ok: "bg-green-100 text-green-800",
  error: "bg-destructive/10 text-destructive",
  skipped: "bg-yellow-100 text-yellow-800",
};

export function RunDetailPanel({
  run,
  job,
  onClose,
  onRunNow,
  onEdit,
}: {
  run: CronRunEntry;
  job: CronJob | undefined;
  onClose: () => void;
  onRunNow: () => void;
  onEdit: () => void;
}) {
  const [promptOpen, setPromptOpen] = useState(false);
  const jobDeleted = !job;

  // Reuse the same chat.history fetch (SWR dedupes with RunTranscript's call)
  // so we can derive the first user message to display in the prompt accordion.
  const { data: transcriptData } = useGatewayRpc<{ messages?: unknown[] }>(
    run.sessionKey ? "chat.history" : null,
    run.sessionKey ? { sessionKey: run.sessionKey, limit: 200 } : undefined,
  );
  const projectedMessages = projectSessionMessages(transcriptData?.messages);
  // Scope the prompt lookup to messages from this run so multi-run sessions
  // (non-isolated cron jobs that share a sessionKey across runs) don't
  // surface the very first prompt the session ever saw.
  const firstUserMsg = firstUserMessage(
    projectedMessages,
    run.triggeredAtMs,
    run.completedAtMs,
  );

  const payloadText = job
    ? job.payload.kind === "agentTurn"
      ? job.payload.message
      : job.payload.text
    : undefined;
  const displayedPrompt = firstUserMsg ?? payloadText;
  const promptEditedSinceRun =
    job !== undefined &&
    job.updatedAtMs > run.triggeredAtMs &&
    firstUserMsg === undefined;

  const copyPrompt = () => {
    navigator.clipboard.writeText(displayedPrompt ?? "").catch(() => {});
  };

  return (
    <div className="flex flex-col h-full bg-[#faf7f2] border-l border-[#e0dbd0]">
      <div className="flex items-center gap-3 px-4 h-14 border-b border-[#e0dbd0]">
        <span
          className={`px-2 py-0.5 rounded text-xs uppercase ${
            STATUS_PILL[run.status] ?? "bg-[#f3efe6] text-[#8a8578]"
          }`}
        >
          {run.status}
        </span>
        <span className="text-sm">{formatAbsoluteTime(run.triggeredAtMs)}</span>
        {run.durationMs !== undefined && (
          <span className="text-xs text-[#8a8578]">
            · {formatDuration(run.durationMs)}
          </span>
        )}
        {jobDeleted && (
          <span className="px-2 py-0.5 rounded text-xs uppercase bg-[#f3efe6] text-[#8a8578]">
            (deleted)
          </span>
        )}
        <div className="flex-1" />
        <Button size="sm" variant="outline" onClick={onRunNow} disabled={jobDeleted}>
          <Play className="h-3 w-3 mr-1" /> Run now
        </Button>
        <Button size="sm" variant="outline" onClick={onEdit} disabled={jobDeleted}>
          <Pencil className="h-3 w-3 mr-1" /> Edit job
        </Button>
        <Button size="sm" variant="outline" onClick={copyPrompt}>
          <Copy className="h-3 w-3 mr-1" /> Copy prompt
        </Button>
        <button
          onClick={onClose}
          className="text-[#8a8578] hover:text-[#1a1a1a]"
          aria-label="Close run details"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      {run.status === "error" && run.error && (
        <div className="mx-4 my-3 p-3 rounded bg-destructive/10 border border-destructive/20 text-sm">
          <div className="text-destructive font-medium">Run failed</div>
          <div className="text-destructive mt-1">{run.error}</div>
        </div>
      )}

      <div className="px-4 py-3 border-b border-[#e0dbd0]">
        <button
          onClick={() => setPromptOpen((v) => !v)}
          className="flex items-center gap-1 text-sm text-[#1a1a1a]"
          aria-expanded={promptOpen}
        >
          {promptOpen ? (
            <ChevronDown className="h-4 w-4" />
          ) : (
            <ChevronRight className="h-4 w-4" />
          )}
          Prompt
          {promptEditedSinceRun && (
            <span className="ml-2 text-xs text-[#8a8578]">
              (job edited since this run)
            </span>
          )}
        </button>
        {promptOpen && (
          <pre className="mt-2 text-sm whitespace-pre-wrap font-mono text-[#1a1a1a] bg-[#f3efe6] p-3 rounded">
            {displayedPrompt ?? "—"}
          </pre>
        )}
      </div>

      <div className="flex-1 overflow-y-auto min-h-0">
        <RunTranscript
          sessionKey={run.sessionKey}
          afterTs={run.triggeredAtMs}
          beforeTs={run.completedAtMs ?? (run.durationMs != null ? run.triggeredAtMs + run.durationMs : undefined)}
        />
      </div>

      <RunMetadata run={run} nextRunAtMs={job?.state.nextRunAtMs} />
    </div>
  );
}
