// apps/frontend/src/components/teams/issues/IssueProperties.tsx

// Ported from upstream Paperclip's pages/IssueDetail.tsx sidebar block
// (paperclip/ui/src/pages/IssueDetail.tsx) (MIT, (c) 2025 Paperclip AI).
// v2: assignee + project rows are now interactive pickers; status +
// priority remain read-only here (they live in the IssueHeader's pickers).
// See plan at docs/superpowers/plans/2026-05-09-teams-issue-pickers-v2.md

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
import { AssigneePicker } from "./pickers/AssigneePicker";
import { ProjectPicker } from "./pickers/ProjectPicker";

export interface IssuePropertiesProps {
  issue: Issue;
  agents?: CompanyAgent[];
  members?: CompanyMember[];
  projects?: IssueProject[];
  onAssigneeChange: (agentId: string | null) => void;
  onProjectChange: (projectId: string | null) => void;
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

export function IssueProperties({
  issue,
  agents = [],
  projects = [],
  onAssigneeChange,
  onProjectChange,
  className,
}: IssuePropertiesProps) {
  // Merge denormalized issue.project into the projects list so the trigger
  // shows the right name even before the /teams/projects fetch resolves.
  const projectsWithDenorm = (() => {
    const base = projects.slice();
    if (issue.project && !base.some((p) => p.id === issue.project!.id)) {
      base.unshift(issue.project);
    }
    return base;
  })();
  const projectId = issue.project?.id ?? null;

  const labels = issue.labels ?? [];

  return (
    <dl className={cn("flex flex-col", className)}>
      <PropertyRow label="Status">
        <span className="inline-flex items-center gap-2">
          <StatusIcon status={issue.status} />
          <span className="capitalize">{issue.status.replace(/_/g, " ")}</span>
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
        <AssigneePicker
          agents={agents}
          selectedAgentId={issue.assigneeAgentId ?? null}
          onChange={onAssigneeChange}
        />
      </PropertyRow>
      <PropertyRow label="Project">
        <ProjectPicker
          projects={projectsWithDenorm}
          selectedProjectId={projectId}
          onChange={onProjectChange}
        />
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
