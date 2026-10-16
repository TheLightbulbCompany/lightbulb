// Ported from upstream Paperclip's KanbanBoard inner card
// (paperclip/ui/src/components/KanbanBoard.tsx) (MIT, (c) 2025 Paperclip AI).
// Editorial retheme: bg-card → editorial-card, blue live dot → forest, ink text.

"use client";

import Link from "next/link";
import { useSortable } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { cn } from "@/lib/utils";
import { teamsDetailUrl } from "@/lib/shellNav";
import { useInlineTeamsNav } from "@/components/chat/InlineTeamsNav";
import { PriorityIcon } from "@/components/teams/shared/components/PriorityIcon";
import { Identity } from "@/components/teams/shared/components/Identity";
import { LiveRunDot } from "@/components/teams/shared/components/LiveRunDot";
import { agentTint } from "@/lib/agentTint";
import type { CompanyMember, Issue } from "@/components/teams/shared/types";

export interface KanbanAgent {
  id: string;
  name: string;
}

export interface KanbanCardProps {
  issue: Issue;
  agents?: KanbanAgent[];
  /** `/members` roster, to name a human assignee (`assigneeUserId` holds the
   * member's Paperclip user id, not the Clerk id). */
  members?: CompanyMember[];
  isLive?: boolean;
}

interface CardContentProps {
  issue: Issue;
  agents?: KanbanAgent[];
  members?: CompanyMember[];
  isLive?: boolean;
  isDragging?: boolean;
}

// Linear-style assignee mark: a 20px circle in the card's top-right. Agents get
// the same tinted initials the messaging roster uses (the agent wire shape has
// no avatar URL — `icon` is an opaque picker name), people get their photo or
// initials, and an unassigned task gets a dashed empty circle, so every card
// answers "whose is this?" at a glance. The full name rides on aria-label +
// title (Identity's avatarOnly mode).
function CardAssignee({
  issue,
  agents,
  members,
}: Pick<CardContentProps, "issue" | "agents" | "members">) {
  if (issue.assigneeAgentId) {
    const name =
      agents?.find((a) => a.id === issue.assigneeAgentId)?.name ?? "Unknown agent";
    return (
      <Identity
        name={name}
        avatarOnly
        size="xs"
        tint={agentTint(issue.assigneeAgentId)}
        className="shrink-0"
      />
    );
  }
  if (issue.assigneeUserId) {
    const member = members?.find((m) => m.paperclipUserId === issue.assigneeUserId);
    return (
      <Identity
        name={member?.name || member?.email || "Unknown person"}
        avatarUrl={member?.imageUrl}
        avatarOnly
        size="xs"
        className="shrink-0"
      />
    );
  }
  return (
    <span
      role="img"
      aria-label="Unassigned"
      title="Unassigned"
      className="size-5 shrink-0 rounded-full border border-dashed"
      style={{ borderColor: "var(--midnight-muted)" }}
    />
  );
}

// Always link by `id` — the rest of the inbox/panels surface (InboxPage,
// IssuesPanel) does the same, and the BFF's upstream /api/issues/{id} read
// expects the internal id, NOT the human-readable identifier (e.g. ENG-42).
function issueHref(issue: Issue): string {
  return teamsDetailUrl("issue", issue.id);
}

function CardContent({ issue, agents, members, isLive, isDragging }: CardContentProps) {
  const nav = useInlineTeamsNav();

  return (
    <Link
      href={issueHref(issue)}
      prefetch={false}
      className="block no-underline"
      style={{ color: "var(--midnight)" }}
      // Native anchors are browser-draggable by default, which hijacks the
      // pointer and prevents dnd-kit's PointerSensor from starting the
      // sortable drag — so the card never moves. Disable native drag so the
      // kanban drag-to-change-status works; the onClick guard below still
      // suppresses navigation when a drag just happened.
      draggable={false}
      onClick={(e) => {
        // A drag just happened — suppress navigation.
        if (isDragging) {
          e.preventDefault();
          return;
        }
        // Inside the /chat shell, route a plain left-click to the inline
        // detail view; keep native <Link> behavior for modified / non-primary
        // clicks (new tab/window). Without a nav provider the href — the
        // canonical in-shell URL — drives navigation natively.
        if (!nav) return;
        if (
          e.defaultPrevented ||
          e.button !== 0 ||
          e.metaKey ||
          e.ctrlKey ||
          e.shiftKey ||
          e.altKey
        ) {
          return;
        }
        e.preventDefault();
        nav.openDetail("issue-detail", issue.id);
      }}
    >
      <div className="flex items-start gap-1.5 mb-1.5">
        <span
          className="text-caption font-mono shrink-0"
          style={{ color: "var(--midnight-muted)" }}
        >
          {issue.identifier ?? issue.id.slice(0, 8)}
        </span>
        {isLive && (
          <LiveRunDot testId="kanban-card-live-dot" className="mt-0.5" />
        )}
        <span className="ml-auto flex shrink-0">
          <CardAssignee issue={issue} agents={agents} members={members} />
        </span>
      </div>
      <p
        className={cn("text-ui leading-snug line-clamp-2", issue.priority && "mb-2")}
        style={{ color: "var(--midnight)" }}
      >
        {issue.title}
      </p>
      {issue.priority && (
        <div className="flex items-center gap-2">
          <PriorityIcon priority={issue.priority} />
        </div>
      )}
    </Link>
  );
}

// Sortable card rendered inside a column. Registers a draggable instance
// with dnd-kit keyed by issue.id.
export function KanbanCard({ issue, agents, members, isLive }: KanbanCardProps) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: issue.id, data: { issue } });

  const style: React.CSSProperties = {
    transform: CSS.Transform.toString(transform),
    transition,
  };

  return (
    <div
      ref={setNodeRef}
      style={style}
      data-kanban-card-id={issue.id}
      className={cn(
        "editorial-card p-2.5 transition-shadow hover:shadow-sm",
        isDragging && "opacity-30",
      )}
    >
      {/* Drag (pointer or keyboard: focus, Space, arrows, Space) is how a
          card changes status on the board — no per-card status picker, same
          as upstream. The detail view still has the full status menu. */}
      <div
        {...attributes}
        {...listeners}
        className="cursor-grab active:cursor-grabbing"
      >
        <CardContent
          issue={issue}
          agents={agents}
          members={members}
          isLive={isLive}
          isDragging={isDragging}
        />
      </div>
    </div>
  );
}

// Presentational-only variant for the DragOverlay. Does NOT call useSortable
// so it can't double-register the active card's id and confuse dnd-kit's
// collision detection.
export function KanbanCardOverlay({ issue, agents, members, isLive }: KanbanCardProps) {
  return (
    <div
      data-kanban-card-overlay={issue.id}
      className="editorial-card p-2.5 cursor-grab shadow-lg ring-1"
    >
      <CardContent issue={issue} agents={agents} members={members} isLive={isLive} />
    </div>
  );
}
