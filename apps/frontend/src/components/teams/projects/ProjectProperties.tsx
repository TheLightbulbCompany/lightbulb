// apps/frontend/src/components/teams/projects/ProjectProperties.tsx

// Ported from upstream Paperclip's pages/ProjectDetail.tsx properties block
// (paperclip/ui/src/pages/ProjectDetail.tsx) (MIT, © 2025 Paperclip AI).
// Mirrors IssueProperties.tsx / GoalProperties.tsx PropertyRow layout verbatim.
// All rows are read-only summaries; editing (name/description/budget) lives in
// the Overview + Budget tabs. Drops upstream's icon/color, budget-utilization
// gauge, and membership rows (no slim-BFF data for them).

"use client";

import type { ReactNode } from "react";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { formatCents } from "@/components/teams/shared/lib/money";
import { cn } from "@/lib/utils";
import type { Project } from "@/components/teams/shared/types";

export interface ProjectPropertiesProps {
  project: Project;
  /** Count of issues scoped to this project (the Issues tab total). */
  issueCount: number;
  className?: string;
}

function PropertyRow({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div
      className="grid grid-cols-[100px_1fr] gap-3 items-baseline py-2 text-sm"
      style={{
        color: "var(--ink-soft)",
        borderBottom: "1px solid var(--edge)",
      }}
    >
      <dt className="section-label" style={{ marginTop: 0, marginBottom: 0 }}>
        <b>{label}</b>
      </dt>
      <dd className="min-w-0 truncate">{children}</dd>
    </div>
  );
}

const EMPTY_DASH_STYLE = { color: "var(--mute)" } as const;

export function ProjectProperties({
  project,
  issueCount,
  className,
}: ProjectPropertiesProps) {
  const budget = project.budgetMonthlyCents;

  return (
    <dl className={cn("flex flex-col", className)}>
      {project.status && (
        <PropertyRow label="Status">
          <span className="capitalize">{project.status.replace(/_/g, " ")}</span>
        </PropertyRow>
      )}
      <PropertyRow label="Budget">
        {typeof budget === "number" && budget > 0 ? (
          <span className="tabular-nums" style={{ color: "var(--ink)" }}>
            {formatCents(budget)}
            <span className="ml-1 text-xs" style={{ color: "var(--mute)" }}>
              / mo
            </span>
          </span>
        ) : (
          <span data-empty="true" style={EMPTY_DASH_STYLE}>
            No cap
          </span>
        )}
      </PropertyRow>
      <PropertyRow label="Issues">
        <span className="tabular-nums" style={{ color: "var(--ink)" }}>
          {issueCount}
        </span>
      </PropertyRow>
      <PropertyRow label="Created">
        {project.createdAt ? (
          <time dateTime={project.createdAt} style={{ color: "var(--mute)" }}>
            {timeAgo(project.createdAt)}
          </time>
        ) : (
          <span data-empty="true" style={EMPTY_DASH_STYLE}>
            —
          </span>
        )}
      </PropertyRow>
      <PropertyRow label="Updated">
        {project.updatedAt ? (
          <time dateTime={project.updatedAt} style={{ color: "var(--mute)" }}>
            {timeAgo(project.updatedAt)}
          </time>
        ) : (
          <span data-empty="true" style={EMPTY_DASH_STYLE}>
            —
          </span>
        )}
      </PropertyRow>
    </dl>
  );
}
