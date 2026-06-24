// apps/frontend/src/components/teams/projects/ProjectProperties.tsx

// Ported from upstream Paperclip's pages/ProjectDetail.tsx properties block
// (paperclip/ui/src/pages/ProjectDetail.tsx) (MIT, © 2025 Paperclip AI).
// Mirrors IssueProperties.tsx / GoalProperties.tsx PropertyRow layout verbatim.
// All rows are read-only summaries; editing (name/description) lives in the
// Overview tab. Drops upstream's icon/color and membership rows (no slim-BFF
// data for them). No budget row — upstream has no project-budget field (see
// shared/types.ts).

"use client";

import { useState, type ReactNode } from "react";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { cn } from "@/lib/utils";
import {
  PROJECT_STATUSES,
  type Project,
  type ProjectStatus,
} from "@/components/teams/shared/types";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Button } from "@/components/ui/button";

export interface ProjectPropertiesProps {
  project: Project;
  /** Count of issues scoped to this project (the Issues tab total). */
  issueCount: number;
  /** Persist a new lifecycle status (PATCH `{ status }`). Mirrors the
   * GoalProperties status/level inline pickers. */
  onStatusChange: (status: ProjectStatus) => void;
  className?: string;
}

function statusLabel(status: string): string {
  return status.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Inline status picker — a Popover of PROJECT_STATUSES, mirroring
 * GoalProperties' LevelPicker so the project-detail aside edits status in
 * place rather than routing through a separate dialog. */
function StatusPicker({
  status,
  onChange,
}: {
  status: ProjectStatus;
  onChange: (status: ProjectStatus) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="cursor-pointer hover:opacity-80 transition-opacity"
          style={{ color: "var(--ink)" }}
        >
          {statusLabel(status)}
        </button>
      </PopoverTrigger>
      <PopoverContent
        className="w-44 p-1"
        align="start"
        style={{
          background: "var(--cream)",
          border: "1px solid var(--edge)",
          color: "var(--ink)",
        }}
      >
        {PROJECT_STATUSES.map((s) => (
          <Button
            key={s}
            variant="ghost"
            size="sm"
            className={cn(
              "w-full justify-start text-xs",
              s === status && "bg-[var(--cream-deeper)]",
            )}
            onClick={() => {
              onChange(s);
              setOpen(false);
            }}
          >
            {statusLabel(s)}
          </Button>
        ))}
      </PopoverContent>
    </Popover>
  );
}

/** Upstream defaults a project to "backlog" when status is absent; reflect
 * that in the picker so the row is always editable (not just when the row
 * happens to carry a status). */
function asProjectStatus(status: string | null | undefined): ProjectStatus {
  return (PROJECT_STATUSES as readonly string[]).includes(status ?? "")
    ? (status as ProjectStatus)
    : "backlog";
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
  onStatusChange,
  className,
}: ProjectPropertiesProps) {
  return (
    <dl className={cn("flex flex-col", className)}>
      <PropertyRow label="Status">
        <StatusPicker
          status={asProjectStatus(project.status)}
          onChange={onStatusChange}
        />
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
