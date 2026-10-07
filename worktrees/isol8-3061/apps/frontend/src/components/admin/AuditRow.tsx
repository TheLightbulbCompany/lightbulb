import * as React from "react";
import Link from "next/link";

import { cn } from "@/lib/utils";
import { formatRelativeTime } from "@/app/admin/_lib/time";

export interface AuditEntry {
  admin_user_id: string;
  /** Composite sort key: `{ISO8601}#{ulid}`. */
  timestamp_action_id: string;
  target_user_id: string;
  /** Dotted action name, e.g. `container.reprovision`. */
  action: string;
  result: "success" | "error";
  audit_status?: "written" | "panic";
  http_status: number;
  elapsed_ms: number;
  error_message?: string;
}

export interface AuditRowProps {
  entry: AuditEntry;
  /** Optional row click handler; renders the row as a button when present. */
  onClick?: () => void;
}

function splitTimestamp(compositeKey: string): string {
  const hashIdx = compositeKey.indexOf("#");
  return hashIdx === -1 ? compositeKey : compositeKey.slice(0, hashIdx);
}

/**
 * One row of the admin audit log table. Server-Component-friendly.
 */
export function AuditRow({ entry, onClick }: AuditRowProps) {
  const timestamp = splitTimestamp(entry.timestamp_action_id);
  const when = formatRelativeTime(timestamp);
  const isPanic = entry.audit_status === "panic";
  const isError = entry.result === "error";

  const Wrapper: React.ElementType = onClick ? "button" : "div";
  // A nested anchor inside a button wrapper would be invalid HTML, so the
  // target-user link only renders on plain rows.
  const linkTarget = !onClick && entry.target_user_id ? entry.target_user_id : null;

  return (
    <Wrapper
      type={onClick ? "button" : undefined}
      onClick={onClick}
      title={`HTTP ${entry.http_status} \u00b7 ${entry.elapsed_ms}ms${entry.error_message ? ` \u00b7 ${entry.error_message}` : ""}`}
      className={cn(
        "grid w-full grid-cols-[180px_minmax(180px,1fr)_minmax(160px,1fr)_auto_auto] items-center gap-3 rounded-md border border-border bg-card px-3 py-2 text-left text-xs",
        onClick && "cursor-pointer transition-colors hover:bg-accent",
      )}
      data-result={entry.result}
      data-audit-status={entry.audit_status ?? "written"}
    >
      <time
        className="font-mono text-muted-foreground"
        dateTime={when.iso ?? timestamp}
        title={when.iso ?? undefined}
        suppressHydrationWarning
      >
        {when.label}
      </time>
      <span className="truncate font-mono text-foreground">{entry.action}</span>
      {linkTarget ? (
        <Link
          href={`/admin/users/${encodeURIComponent(linkTarget)}`}
          className="truncate text-info hover:underline"
          title={linkTarget}
        >
          {linkTarget}
        </Link>
      ) : (
        <span className="truncate text-muted-foreground">{entry.target_user_id}</span>
      )}
      <span
        className={cn(
          "rounded-sm px-2 py-0.5 text-2xs font-semibold uppercase tracking-caps",
          isError
            ? "border border-err text-err"
            : "border border-ok text-ok",
        )}
      >
        {entry.result}
      </span>
      {isPanic ? (
        <span
          className="rounded-sm border border-warn px-2 py-0.5 text-2xs font-semibold uppercase tracking-caps text-warn"
          title="Audit row failed to write to DynamoDB; see CloudWatch"
        >
          {"audit panic \u2014 see CloudWatch"}
        </span>
      ) : (
        <span aria-hidden="true" />
      )}
    </Wrapper>
  );
}
