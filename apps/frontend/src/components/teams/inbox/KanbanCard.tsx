// Ported from upstream Paperclip's KanbanBoard inner card
// (paperclip/ui/src/components/KanbanBoard.tsx) (MIT, (c) 2025 Paperclip AI).
// Editorial retheme: bg-card → editorial-card, blue live dot → forest, ink text.

"use client";

import Link from "next/link";
import { useSortable } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { cn } from "@/lib/utils";
import { PriorityIcon } from "@/components/teams/shared/components/PriorityIcon";
import { Identity } from "@/components/teams/shared/components/Identity";
import type { Issue } from "@/components/teams/shared/types";

export interface KanbanAgent {
  id: string;
  name: string;
}

export interface KanbanCardProps {
  issue: Issue;
  agents?: KanbanAgent[];
  isLive?: boolean;
  isOverlay?: boolean;
}

export function KanbanCard({ issue, agents, isLive, isOverlay }: KanbanCardProps) {
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

  const agentName = (id: string | null | undefined) => {
    if (!id || !agents) return null;
    return agents.find((a) => a.id === id)?.name ?? null;
  };

  // Always link by `id` — the rest of the inbox/panels surface (InboxPage,
  // IssuesPanel) does the same, and the BFF forwards /teams/issues/{id} to
  // upstream /api/issues/{id} which expects the internal id, NOT the
  // human-readable identifier (e.g. ENG-42).
  const href = `/teams/issues/${issue.id}`;

  return (
    <div
      ref={setNodeRef}
      style={style}
      {...attributes}
      {...listeners}
      data-kanban-card-id={issue.id}
      className={cn(
        "editorial-card p-2.5 cursor-grab active:cursor-grabbing transition-shadow",
        isDragging && !isOverlay && "opacity-30",
        isOverlay ? "shadow-lg ring-1" : "hover:shadow-sm",
      )}
    >
      <Link
        href={href}
        className="block no-underline"
        style={{ color: "var(--ink)" }}
        onClick={(e) => {
          if (isDragging) e.preventDefault();
        }}
      >
        <div className="flex items-start gap-1.5 mb-1.5">
          <span
            className="text-xs font-mono shrink-0"
            style={{ color: "var(--mute)" }}
          >
            {issue.identifier ?? issue.id.slice(0, 8)}
          </span>
          {isLive && (
            <span
              data-testid="kanban-card-live-dot"
              className="relative flex h-2 w-2 shrink-0 mt-0.5"
            >
              <span
                className="animate-pulse absolute inline-flex h-full w-full rounded-full opacity-75"
                style={{ backgroundColor: "var(--forest)" }}
              />
              <span
                className="relative inline-flex rounded-full h-2 w-2"
                style={{ backgroundColor: "var(--forest)" }}
              />
            </span>
          )}
        </div>
        <p
          className="text-sm leading-snug line-clamp-2 mb-2"
          style={{ color: "var(--ink)" }}
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
                  className="text-xs font-mono"
                  style={{ color: "var(--mute)" }}
                >
                  {issue.assigneeAgentId.slice(0, 8)}
                </span>
              );
            })()}
        </div>
      </Link>
    </div>
  );
}
