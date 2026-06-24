// apps/frontend/src/components/teams/issues/IssueProperties.tsx

// Ported from upstream Paperclip's pages/IssueDetail.tsx sidebar block
// (paperclip/ui/src/pages/IssueDetail.tsx) (MIT, (c) 2025 Paperclip AI).
// The rail is the single editing surface for every task field — status,
// priority, assignee, and project rows are all interactive pickers — so each
// value has exactly one place it can be changed (the header stays a clean
// title-only masthead).

"use client";

import type { ReactNode } from "react";
import Link from "next/link";
import { StatusIcon } from "@/components/teams/shared/components/StatusIcon";
import { PriorityIcon } from "@/components/teams/shared/components/PriorityIcon";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { cn } from "@/lib/utils";
import type {
  Issue,
  IssueStatus,
  IssuePriority,
  CompanyAgent,
  CompanyMember,
  IssueProject,
} from "@/components/teams/shared/types";
import { AssigneePicker } from "./pickers/AssigneePicker";
import { ProjectPicker } from "./pickers/ProjectPicker";
import { IssueRelations } from "./IssueRelations";

export interface IssuePropertiesProps {
  issue: Issue;
  agents?: CompanyAgent[];
  members?: CompanyMember[];
  projects?: IssueProject[];
  onStatusChange: (status: IssueStatus) => void;
  onPriorityChange: (priority: IssuePriority) => void;
  onAssigneeChange: (agentId: string | null) => void;
  onProjectChange: (projectId: string | null) => void;
  /** Receives the FULL desired blocker-id set (replace semantics). */
  onBlockedByChange: (ids: string[]) => void;
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

// Shared chip styling for the Labels and Related-Tasks rows so the read-only
// pills read identically across the rail.
const CHIP_STYLE = {
  background: "var(--cream-deeper)",
  color: "var(--ink-soft)",
  border: "1px solid var(--edge)",
} as const;

function EmptyDash() {
  return (
    <span data-empty="true" style={EMPTY_DASH_STYLE}>
      —
    </span>
  );
}

// The children endpoint returns either the `{ issues }` envelope (used by
// SubIssuesSection) or, on some BFF paths, a bare array — accept both so the
// count stays correct regardless of which shape the proxy forwards.
type ChildrenResponse = { issues: Issue[] } | Issue[];

function childrenCount(data: ChildrenResponse | undefined): number {
  if (!data) return 0;
  if (Array.isArray(data)) return data.length;
  return data.issues?.length ?? 0;
}

/** Read-only Parent row. Rendered only when the issue has a parent, so the
 * fetch never fires for top-level tasks (mirrors IssueParentBreadcrumb). */
function ParentRow({ parentId }: { parentId: string }) {
  const { read } = useTeamsApi();
  const { data: parent } = read<Issue>(`/issues/${parentId}`);
  const label = parent
    ? (parent.identifier ?? parent.id.slice(0, 8))
    : parentId.slice(0, 8);
  const title = parent?.title?.trim() ?? "";
  return (
    <PropertyRow label="Parent">
      <Link
        href={`/teams/issues/${parentId}`}
        className="inline-flex min-w-0 items-baseline gap-1.5 hover:underline"
        style={{ color: "var(--forest)" }}
      >
        <span className="font-mono text-xs shrink-0">{label}</span>
        {title && <span className="truncate">{title}</span>}
      </Link>
    </PropertyRow>
  );
}

/** Read-only Sub-tasks count row. Always fetches children (cheap, and the
 * detail body fetches the same list, so SWR dedupes the request). */
function SubTasksRow({ issueId }: { issueId: string }) {
  const { read } = useTeamsApi();
  const { data } = read<ChildrenResponse>(`/issues/${issueId}/children`);
  const count = childrenCount(data);
  return (
    <PropertyRow label="Sub-tasks">
      {count > 0 ? (
        <span style={{ color: "var(--ink-soft)" }}>
          {count} {count === 1 ? "sub-task" : "sub-tasks"}
        </span>
      ) : (
        <EmptyDash />
      )}
    </PropertyRow>
  );
}

export function IssueProperties({
  issue,
  agents = [],
  projects = [],
  onStatusChange,
  onPriorityChange,
  onAssigneeChange,
  onProjectChange,
  onBlockedByChange,
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
  const relatedIdentifiers = issue.referencedIssueIdentifiers ?? [];

  return (
    <dl className={cn("flex flex-col", className)}>
      <PropertyRow label="Status">
        <StatusIcon
          status={issue.status}
          blockerAttention={
            typeof issue.blockerAttention === "object"
              ? issue.blockerAttention
              : null
          }
          onChange={(s) => onStatusChange(s as IssueStatus)}
          showLabel
        />
      </PropertyRow>
      <PropertyRow label="Priority">
        {issue.priority ? (
          <PriorityIcon
            priority={issue.priority}
            onChange={(p) => onPriorityChange(p)}
            showLabel
          />
        ) : (
          <EmptyDash />
        )}
      </PropertyRow>
      {/* Labels sits directly after Priority to match upstream's rail order
          (Status, Priority, Labels, Assignee, Project, …). */}
      <PropertyRow label="Labels">
        {labels.length > 0 ? (
          <span className="flex flex-wrap gap-1">
            {labels.map((l) => (
              <span
                key={l.id}
                className="rounded px-1.5 py-0.5 text-xs"
                style={CHIP_STYLE}
              >
                {l.name}
              </span>
            ))}
          </span>
        ) : (
          <EmptyDash />
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
      {issue.parentId ? (
        <ParentRow parentId={issue.parentId} />
      ) : (
        <PropertyRow label="Parent">
          <EmptyDash />
        </PropertyRow>
      )}
      <IssueRelations issue={issue} onBlockedByChange={onBlockedByChange} />
      <SubTasksRow issueId={issue.id} />
      <PropertyRow label="Related Tasks">
        {relatedIdentifiers.length > 0 ? (
          <span className="flex flex-wrap gap-1">
            {relatedIdentifiers.map((identifier) => (
              <span
                key={identifier}
                className="rounded px-1.5 py-0.5 text-xs font-mono"
                style={CHIP_STYLE}
              >
                {identifier}
              </span>
            ))}
          </span>
        ) : (
          <EmptyDash />
        )}
      </PropertyRow>
      <PropertyRow label="Created">
        {issue.createdAt ? (
          <time dateTime={issue.createdAt} style={{ color: "var(--mute)" }}>
            {timeAgo(issue.createdAt)}
          </time>
        ) : (
          <EmptyDash />
        )}
      </PropertyRow>
      <PropertyRow label="Updated">
        {issue.updatedAt ? (
          <time dateTime={issue.updatedAt} style={{ color: "var(--mute)" }}>
            {timeAgo(issue.updatedAt)}
          </time>
        ) : (
          <EmptyDash />
        )}
      </PropertyRow>
    </dl>
  );
}
