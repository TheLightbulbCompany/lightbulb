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
  ERROR: "border border-err text-err",
  WARN: "border border-warn text-warn",
  INFO: "border border-info text-info",
  DEBUG: "border border-border text-muted-foreground",
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
      className="rounded-md border border-border bg-card"
      data-expanded={expanded ? "true" : "false"}
    >
      <button
        type="button"
        onClick={() => setExpanded((prev) => !prev)}
        aria-expanded={expanded}
        aria-label={expanded ? "Collapse log entry" : "Expand log entry"}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs hover:bg-accent"
      >
        {expanded ? (
          <ChevronDown className="size-3 shrink-0 text-muted-foreground" aria-hidden />
        ) : (
          <ChevronRight className="size-3 shrink-0 text-muted-foreground" aria-hidden />
        )}
        {/* suppressHydrationWarning: the relative label is a function of
            Date.now(), so SSR and hydration can legitimately differ by a
            minute tick. */}
        <time
          className="font-mono text-muted-foreground"
          dateTime={when.iso ?? entry.timestamp}
          title={when.iso ?? undefined}
          suppressHydrationWarning
        >
          {when.label}
        </time>
        <span
          className={cn(
            "rounded-sm px-1.5 py-0.5 text-2xs font-semibold uppercase tracking-caps",
            levelBadgeClass(entry.level),
          )}
          data-level={levelLabel}
        >
          {levelLabel}
        </span>
        <span className="flex-1 truncate font-mono text-foreground">
          {truncated}
        </span>
      </button>

      {expanded ? (
        <div className="space-y-2 border-t border-border px-3 py-3">
          {entry.correlation_id ? (
            <div className="text-xs text-muted-foreground">
              <span className="font-semibold text-foreground">
                correlation_id:
              </span>{" "}
              <span className="font-mono text-foreground">
                {entry.correlation_id}
              </span>
            </div>
          ) : null}
          {entry.raw_json ? (
            <CodeBlock value={entry.raw_json} language="json" maxHeight={320} />
          ) : (
            <p className="text-xs italic text-muted-foreground">
              No raw JSON payload.
            </p>
          )}
        </div>
      ) : null}
    </div>
  );
}
