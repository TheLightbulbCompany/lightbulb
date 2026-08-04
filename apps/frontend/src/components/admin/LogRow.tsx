"use client";

import * as React from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

import { cn, truncate } from "@/lib/utils";
import { CodeBlock } from "@/components/admin/CodeBlock";
import { formatRelativeTime } from "@/app/admin/_lib/time";

export interface LogEntry {
  timestamp: string;
  level: "ERROR" | "WARN" | "INFO" | "DEBUG" | string | null;
  message: string;
  correlation_id?: string | null;
  raw_json: Record<string, unknown> | null;
}

export interface LogRowProps {
  entry: LogEntry;
}

const MESSAGE_TRUNCATE = 200;

const LEVEL_BADGE_CLASS: Record<string, string> = {
  ERROR: "bg-red-500/15 text-red-300",
  WARN: "bg-amber-500/15 text-amber-300",
  INFO: "bg-sky-500/15 text-sky-300",
  DEBUG: "bg-zinc-500/15 text-zinc-300",
};

function levelBadgeClass(level: string | null | undefined): string {
  if (!level) return LEVEL_BADGE_CLASS.DEBUG;
  return LEVEL_BADGE_CLASS[level.toUpperCase()] ?? LEVEL_BADGE_CLASS.DEBUG;
}

/**
 * Expandable log line for the inline CloudWatch viewer on admin pages.
 * Collapsed by default; clicking reveals the correlation id and full raw JSON.
 */
export function LogRow({ entry }: LogRowProps) {
  const [expanded, setExpanded] = React.useState(false);
  const truncated = truncate(entry.message, MESSAGE_TRUNCATE);
  const levelLabel = entry.level ?? "DEBUG";
  const when = formatRelativeTime(entry.timestamp);

  return (
    <div
      className="rounded-md border border-white/5 bg-white/[0.02]"
      data-expanded={expanded ? "true" : "false"}
    >
      <button
        type="button"
        onClick={() => setExpanded((prev) => !prev)}
        aria-expanded={expanded}
        aria-label={expanded ? "Collapse log entry" : "Expand log entry"}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs hover:bg-white/[0.03]"
      >
        {expanded ? (
          <ChevronDown className="size-3 shrink-0 text-zinc-500" aria-hidden />
        ) : (
          <ChevronRight className="size-3 shrink-0 text-zinc-500" aria-hidden />
        )}
        {/* suppressHydrationWarning: the relative label is a function of
            Date.now(), so SSR and hydration can legitimately differ by a
            minute tick. */}
        <time
          className="font-mono text-zinc-400"
          dateTime={when.iso ?? entry.timestamp}
          title={when.iso ?? undefined}
          suppressHydrationWarning
        >
          {when.label}
        </time>
        <span
          className={cn(
            "rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
            levelBadgeClass(entry.level),
          )}
          data-level={levelLabel}
        >
          {levelLabel}
        </span>
        <span className="flex-1 truncate font-mono text-zinc-100">
          {truncated}
        </span>
      </button>

      {expanded ? (
        <div className="space-y-2 border-t border-white/5 px-3 py-3">
          {entry.correlation_id ? (
            <div className="text-xs text-zinc-400">
              <span className="font-semibold text-zinc-300">
                correlation_id:
              </span>{" "}
              <span className="font-mono text-zinc-100">
                {entry.correlation_id}
              </span>
            </div>
          ) : null}
          {entry.raw_json ? (
            <CodeBlock value={entry.raw_json} language="json" maxHeight={320} />
          ) : (
            <p className="text-xs italic text-zinc-500">
              No raw JSON payload.
            </p>
          )}
        </div>
      ) : null}
    </div>
  );
}
