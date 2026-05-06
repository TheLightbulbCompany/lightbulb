// apps/frontend/src/components/teams/issues/IssueProperties.tsx

// Ported from upstream Paperclip's pages/IssueDetail.tsx sidebar block
// (paperclip/ui/src/pages/IssueDetail.tsx) (MIT, (c) 2025 Paperclip AI).
// v1: read-only stack of label/value rows. Mutations live in IssueHeader.
// Drops assignee picker, labels editor, project picker, watcher list, etc.
// See spec at docs/superpowers/specs/2026-05-04-teams-inbox-deep-port-design.md

"use client";

import type { ReactNode } from "react";
import { StatusIcon } from "@/components/teams/shared/components/StatusIcon";
import { PriorityIcon } from "@/components/teams/shared/components/PriorityIcon";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { cn } from "@/lib/utils";
import type {
  Issue,
  CompanyAgent,
  CompanyMember,
  IssueProject,
} from "@/components/teams/shared/types";

export interface IssuePropertiesProps {
  issue: Issue;
  agents?: CompanyAgent[];
  members?: CompanyMember[];
  projects?: IssueProject[];
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
      <dt
        className="section-label"
        style={{ marginTop: 0, marginBottom: 0 }}
      >
        <b>{label}</b>
      </dt>
      <dd className="min-w-0 truncate">{children}</dd>
    </div>
  );
}

const EMPTY_DASH_STYLE = { color: "var(--mute)" } as const;

export function IssueProperties({
  issue,
  agents = [],
  className,
}: IssuePropertiesProps) {
  const assigneeName = issue.assigneeAgentId
    ? (agents.find((a) => a.id === issue.assigneeAgentId)?.name ??
      "Unknown agent")
    : "Unassigned";

  // Project comes denormalized from the BFF as `issue.project`. The
  // `projects` / `members` props are accepted today so callers don't need to
  // refactor when v2 wires assignee/project pickers in.
  const project: IssueProject | null = issue.project ?? null;

  const labels = issue.labels ?? [];

  return (
    <dl className={cn("flex flex-col", className)}>
      <PropertyRow label="Status">
        <span className="inline-flex items-center gap-2">
          <StatusIcon status={issue.status} />
          <span className="capitalize">
            {issue.status.replace(/_/g, " ")}
          </span>
        </span>
      </PropertyRow>
      <PropertyRow label="Priority">
        {issue.priority ? (
          <span className="inline-flex items-center gap-2">
            <PriorityIcon priority={issue.priority} />
            <span className="capitalize">{issue.priority}</span>
          </span>
        ) : (
          <span data-empty="true" style={EMPTY_DASH_STYLE}>
            —
          </span>
        )}
      </PropertyRow>
      <PropertyRow label="Assignee">
        <span style={!issue.assigneeAgentId ? EMPTY_DASH_STYLE : undefined}>
          {assigneeName}
        </span>
      </PropertyRow>
      <PropertyRow label="Project">
        {project ? (
          <span>{project.name}</span>
        ) : (
          <span data-empty="true" style={EMPTY_DASH_STYLE}>
            —
          </span>
        )}
      </PropertyRow>
      <PropertyRow label="Labels">
        {labels.length > 0 ? (
          <span className="flex flex-wrap gap-1">
            {labels.map((l) => (
              <span
                key={l.id}
                className="rounded px-1.5 py-0.5 text-xs"
                style={{
                  background: "var(--cream-deeper)",
                  color: "var(--ink-soft)",
                  border: "1px solid var(--edge)",
                }}
              >
                {l.name}
              </span>
            ))}
          </span>
        ) : (
          <span data-empty="true" style={EMPTY_DASH_STYLE}>
            —
          </span>
        )}
      </PropertyRow>
      <PropertyRow label="Created">
        {issue.createdAt ? (
          <time dateTime={issue.createdAt} style={{ color: "var(--mute)" }}>
            {timeAgo(issue.createdAt)}
          </time>
        ) : (
          <span data-empty="true" style={EMPTY_DASH_STYLE}>
            —
          </span>
        )}
      </PropertyRow>
      <PropertyRow label="Updated">
        {issue.updatedAt ? (
          <time dateTime={issue.updatedAt} style={{ color: "var(--mute)" }}>
            {timeAgo(issue.updatedAt)}
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
