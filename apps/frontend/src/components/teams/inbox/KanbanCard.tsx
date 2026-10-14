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
import type { Issue } from "@/components/teams/shared/types";

export interface KanbanAgent {
  id: string;
  name: string;
}

export interface KanbanCardProps {
  issue: Issue;
  agents?: KanbanAgent[];
  isLive?: boolean;
  // Keyboard/screen-reader alternative to dragging between columns.
  columns?: readonly string[];
  onStatusChange?: (id: string, status: string) => void;
}

interface CardContentProps {
  issue: Issue;
  agents?: KanbanAgent[];
  isLive?: boolean;
  isDragging?: boolean;
}

// Always link by `id` — the rest of the inbox/panels surface (InboxPage,
// IssuesPanel) does the same, and the BFF's upstream /api/issues/{id} read
// expects the internal id, NOT the human-readable identifier (e.g. ENG-42).
function issueHref(issue: Issue): string {
  return teamsDetailUrl("issue", issue.id);
}

function CardContent({ issue, agents, isLive, isDragging }: CardContentProps) {
  const nav = useInlineTeamsNav();
  const agentName = (id: string | null | undefined): string | null => {
    if (!id || !agents) return null;
    return agents.find((a) => a.id === id)?.name ?? null;
  };

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
      </div>
      <p
        className="text-ui leading-snug line-clamp-2 mb-2"
        style={{ color: "var(--midnight)" }}
      >
        {issue.title}
      </p>
      <div className="flex items-center gap-2">
        {issue.priority && <PriorityIcon priority={issue.priority} />}
        {issue.assigneeAgentId &&
          (() => {
            const name = agentName(issue.assigneeAgentId);
            return name ? (
              <Identity name={name} size="xs" />
            ) : (
              <span
                className="text-caption font-mono"
                style={{ color: "var(--midnight-muted)" }}
              >
                {issue.assigneeAgentId.slice(0, 8)}
              </span>
            );
          })()}
      </div>
    </Link>
  );
}

// Sortable card rendered inside a column. Registers a draggable instance
// with dnd-kit keyed by issue.id.
export function KanbanCard({ issue, agents, isLive, columns, onStatusChange }: KanbanCardProps) {
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
      {/* Sortable role/tabIndex live here so the status <select> below is a
          sibling, not nested inside a role="button". */}
      <div
        {...attributes}
        {...listeners}
        className="cursor-grab active:cursor-grabbing"
      >
        <CardContent
          issue={issue}
          agents={agents}
          isLive={isLive}
          isDragging={isDragging}
        />
      </div>
      {columns && onStatusChange && (
        <select
          aria-label={`Status of ${issue.title}`}
          value={issue.status}
          className="mt-2 w-full rounded border bg-transparent text-caption"
          onChange={(e) => onStatusChange(issue.id, e.target.value)}
        >
          {columns.map((s) => (
            <option key={s} value={s}>
              {s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())}
            </option>
          ))}
        </select>
      )}
    </div>
  );
}

// Presentational-only variant for the DragOverlay. Does NOT call useSortable
// so it can't double-register the active card's id and confuse dnd-kit's
// collision detection.
export function KanbanCardOverlay({ issue, agents, isLive }: KanbanCardProps) {
  return (
    <div
      data-kanban-card-overlay={issue.id}
      className="editorial-card p-2.5 cursor-grab shadow-lg ring-1"
    >
      <CardContent issue={issue} agents={agents} isLive={isLive} />
    </div>
  );
}
