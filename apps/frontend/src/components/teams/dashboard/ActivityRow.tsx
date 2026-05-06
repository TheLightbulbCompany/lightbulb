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

  const inner = (
    <div className="space-y-1">
      <div className="flex gap-3">
        <p className="flex-1 min-w-0 truncate">
          <Identity name={actorName} size="xs" className="align-middle" />
          <span className="text-muted-foreground ml-1">{verb}</span>
          {event.entityType && event.entityId && (
            <span className="font-medium ml-1">
              {event.entityType}:{event.entityId}
            </span>
          )}
        </p>
        <span className="text-xs text-muted-foreground shrink-0 pt-0.5">
          {timeAgo(event.createdAt)}
        </span>
      </div>
    </div>
  );

  const classes = cn(
    "px-4 py-2 text-sm",
    link && "cursor-pointer hover:bg-accent/50 transition-colors",
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
