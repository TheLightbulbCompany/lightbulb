// apps/frontend/src/components/teams/dashboard/ActivityRow.tsx

// Ported from upstream Paperclip's ActivityRow
// (paperclip/ui/src/components/ActivityRow.tsx) (MIT, (c) 2025 Paperclip AI).
// Simplified for our BFF shape (no agentMap / entityNameMap / IssueReferenceSummary
// — those upstream maps require sibling reads we don't yet wire). Renders the
// actor identity + verb + entity hint + relative timestamp.
// See spec at docs/superpowers/specs/2026-05-04-teams-ui-parity-roadmap.md

import Link from "next/link";
import { Identity } from "@/components/teams/shared/components/Identity";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { cn } from "@/lib/utils";
import type { ActivityEvent } from "@/components/teams/dashboard/hooks/useActivityFeed";

const VERBS: Record<string, string> = {
  "issue.created": "created",
  "issue.updated": "updated",
  "issue.checked_out": "checked out",
  "issue.released": "released",
  "issue.comment_added": "commented on",
  "issue.commented": "commented on",
  "issue.deleted": "deleted",
  "agent.created": "created",
  "agent.updated": "updated",
  "agent.paused": "paused",
  "agent.resumed": "resumed",
  "agent.terminated": "terminated",
  "heartbeat.invoked": "invoked heartbeat for",
  "heartbeat.cancelled": "cancelled heartbeat for",
  "approval.created": "requested approval",
  "approval.approved": "approved",
  "approval.rejected": "rejected",
  "project.created": "created",
  "project.updated": "updated",
  "project.deleted": "deleted",
  "goal.created": "created",
  "goal.updated": "updated",
  "goal.deleted": "deleted",
};

function entityLink(entityType: string | null | undefined, entityId: string | null | undefined): string | null {
  if (!entityType || !entityId) return null;
  switch (entityType) {
    case "issue":
      return `/teams/issues/${entityId}`;
    case "agent":
      return `/teams/agents/${entityId}`;
    case "project":
      return `/teams/projects/${entityId}`;
    case "goal":
      return `/teams/goals/${entityId}`;
    case "approval":
      return `/teams/approvals/${entityId}`;
    default:
      return null;
  }
}

/**
 * Subtle 6px dot color-coded by entity type; gives the otherwise-uniform
 * activity feed a quick scannable left rail. Mapped onto the editorial
 * palette so the dots stay legible against cream and don't introduce
 * dark-mode-only brand colors.
 */
function entityDotColor(entityType: string | null | undefined): string {
  switch (entityType) {
    case "issue":      return "var(--forest-soft)";
    case "agent":      return "var(--forest)";
    case "approval":   return "var(--warn)";
    case "project":    return "var(--ink-soft)";
    case "goal":       return "var(--mute)";
    case "heartbeat":  return "var(--forest-soft)";
    default:           return "var(--mute-soft)";
  }
}

function actorDisplayName(event: ActivityEvent): string {
  if (event.actorType === "system") return "System";
  if (event.actorType === "agent") return event.actorId ?? "Agent";
  if (event.actorType === "user") return event.actorId ?? "User";
  return event.actorId ?? "Unknown";
}

export interface ActivityRowProps {
  event: ActivityEvent;
  className?: string;
}

export function ActivityRow({ event, className }: ActivityRowProps) {
  const verb = VERBS[event.action] ?? event.action;
  const link = entityLink(event.entityType, event.entityId);
  const actorName = actorDisplayName(event);
  const dotColor = entityDotColor(event.entityType);

  const inner = (
    <div className="flex items-start gap-3">
      <span
        className="mt-[7px] h-1.5 w-1.5 rounded-full shrink-0"
        style={{ backgroundColor: dotColor }}
        aria-hidden
      />
      <p className="flex-1 min-w-0 truncate leading-relaxed" style={{ color: "var(--ink)" }}>
        <Identity name={actorName} size="xs" className="align-middle" />
        <span className="ml-1" style={{ color: "var(--mute)" }}>{verb}</span>
        {event.entityType && event.entityId && (
          <span className="font-medium ml-1" style={{ color: "var(--ink)" }}>
            <span style={{ color: "var(--mute)" }}>{event.entityType}:</span>
            <span className="font-mono text-[0.85em] tracking-tight">{event.entityId}</span>
          </span>
        )}
      </p>
      <span
        className="text-[11px] shrink-0 pt-[3px] tabular-nums font-mono"
        style={{ color: "var(--mute)" }}
      >
        {timeAgo(event.createdAt)}
      </span>
    </div>
  );

  const classes = cn(
    "px-4 py-2.5 text-sm transition-colors",
    link && "cursor-pointer hover:bg-[color:var(--cream-deeper)]",
    className,
  );

  if (link) {
    return (
      <Link href={link} className={cn(classes, "no-underline text-inherit block")}>
        {inner}
      </Link>
    );
  }

  return <div className={classes}>{inner}</div>;
}
