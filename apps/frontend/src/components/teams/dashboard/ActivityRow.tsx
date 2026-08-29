// apps/frontend/src/components/teams/dashboard/ActivityRow.tsx

// Ported from upstream Paperclip's ActivityRow
// (paperclip/ui/src/components/ActivityRow.tsx) (MIT, (c) 2025 Paperclip AI).
// Simplified for our BFF shape (no agentMap / entityNameMap / IssueReferenceSummary
// — those upstream maps require sibling reads we don't yet wire). Renders the
// actor identity + verb + entity hint + relative timestamp.

import Link from "next/link";
import { Identity } from "@/components/teams/shared/components/Identity";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { cn } from "@/lib/utils";
import {
  handleShellLinkClick,
  teamsDetailUrl,
  teamsPanelUrl,
} from "@/lib/shellNav";
import { agentInspectorUrl } from "@/components/teams/shared/lib/agentInspectorUrl";
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

/** Stable empty map so a missing ``agentNames`` prop doesn't allocate a new
 *  Map on every render (which would churn the row's memoization downstream). */
const EMPTY_AGENT_NAMES: ReadonlyMap<string, string> = new Map();

function entityLink(entityType: string | null | undefined, entityId: string | null | undefined): string | null {
  if (!entityType || !entityId) return null;
  switch (entityType) {
    case "issue":
      return teamsDetailUrl("issue", entityId);
    case "agent":
      return agentInspectorUrl(entityId);
    case "project":
      return teamsDetailUrl("project", entityId);
    case "goal":
      return teamsDetailUrl("goal", entityId);
    case "approval":
      // No per-approval detail surface — approvals live in the inbox's
      // "Needs attention" band, so the row links to the list.
      return teamsPanelUrl("inbox");
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
    case "issue":      return "var(--brass)";
    case "agent":      return "var(--midnight)";
    case "approval":   return "var(--warn)";
    case "project":    return "var(--midnight-soft)";
    case "goal":       return "var(--midnight-muted)";
    case "heartbeat":  return "var(--brass)";
    default:           return "var(--midnight-faint)";
  }
}

/** Shorten a raw UUID/id to a scannable slug (`a1b2…ef34`) so it's still
 *  copyable-by-eye when no name resolves, without flooding the row with a
 *  full 36-char id. Short ids (<= 12 chars) pass through unchanged. */
function shortenId(id: string): string {
  if (id.length <= 12) return id;
  return `${id.slice(0, 6)}…${id.slice(-4)}`;
}

/** Resolve the actor's display name.
 *
 * Preference order: BFF-enriched ``actorName`` / ``actorEmail`` (user actors),
 * then the agent roster name (agent actors), then a shortened raw id. ``System``
 * is special-cased. */
function actorDisplayName(
  event: ActivityEvent,
  agentNames: ReadonlyMap<string, string>,
): string {
  if (event.actorType === "system") return "System";
  if (event.actorName) return event.actorName;
  if (event.actorEmail) return event.actorEmail;
  if (event.actorType === "agent") {
    const id = event.actorId;
    if (id) return agentNames.get(id) ?? shortenId(id);
    return "Agent";
  }
  if (event.actorId) return shortenId(event.actorId);
  return event.actorType === "user" ? "User" : "Unknown";
}

/** Entity types that denote an agent (so the entity id resolves to a name). */
function isAgentEntity(entityType: string | null | undefined): boolean {
  return entityType === "agent";
}

export interface ActivityRowProps {
  event: ActivityEvent;
  /** ``agentId → name`` roster map (from DashboardPanel's ``/agents`` read).
   *  Used to resolve agent actors + agent entity references to names. */
  agentNames?: ReadonlyMap<string, string>;
  className?: string;
}

export function ActivityRow({ event, agentNames, className }: ActivityRowProps) {
  const names = agentNames ?? EMPTY_AGENT_NAMES;
  const verb = VERBS[event.action] ?? event.action;
  const link = entityLink(event.entityType, event.entityId);
  const actorName = actorDisplayName(event, names);
  const dotColor = entityDotColor(event.entityType);
  // Resolve the entity reference: agent entities → roster name; everything
  // else keeps its type label + a shortened id (no name source wired yet).
  const entityName =
    isAgentEntity(event.entityType) && event.entityId
      ? names.get(event.entityId)
      : undefined;

  const inner = (
    <div className="flex items-start gap-3">
      <span
        className="mt-[7px] h-1.5 w-1.5 rounded-full shrink-0"
        style={{ backgroundColor: dotColor }}
        aria-hidden
      />
      <p className="flex-1 min-w-0 truncate leading-relaxed" style={{ color: "var(--midnight)" }}>
        <Identity name={actorName} size="xs" className="align-middle" />
        <span className="ml-1" style={{ color: "var(--midnight-muted)" }}>{verb}</span>
        {/* Entity reference. Agent entities resolve to a roster name (passed
            down from DashboardPanel's /agents read); other entity types have
            no name source wired yet, so they keep the "{type}: {shortId}"
            form rather than dumping a full raw UUID. */}
        {event.entityType && event.entityId && (
          <span className="font-medium ml-1" style={{ color: "var(--midnight)" }}>
            {entityName ? (
              entityName
            ) : (
              <>
                <span style={{ color: "var(--midnight-muted)" }}>{event.entityType}:</span>
                <span className="font-mono text-[0.85em]">
                  {shortenId(event.entityId)}
                </span>
              </>
            )}
          </span>
        )}
      </p>
      <span
        className="text-caption shrink-0 pt-[3px] tabular-nums font-mono"
        style={{ color: "var(--midnight-muted)" }}
      >
        {timeAgo(event.createdAt)}
      </span>
    </div>
  );

  const classes = cn(
    "px-4 py-2.5 text-sm transition-colors",
    link && "cursor-pointer hover:bg-[color:var(--cream-deep)]",
    className,
  );

  if (link) {
    return (
      <Link
        href={link}
        prefetch={false}
        onClick={(e) => handleShellLinkClick(e, link)}
        className={cn(classes, "no-underline text-inherit block")}
      >
        {inner}
      </Link>
    );
  }

  return <div className={classes}>{inner}</div>;
}
