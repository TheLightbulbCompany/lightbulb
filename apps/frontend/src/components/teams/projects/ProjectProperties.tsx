// apps/frontend/src/components/teams/projects/ProjectProperties.tsx

// Ported from upstream Paperclip's components/ProjectProperties.tsx
// (paperclip/ui/src/components/ProjectProperties.tsx) (MIT, © 2025 Paperclip AI).
// Mirrors IssueProperties.tsx / GoalProperties.tsx PropertyRow layout verbatim.
// Status is an inline picker and Goals an inline add/remove multi-select; the
// rest are read-only summaries (name/description editing lives in the Overview
// tab). Drops upstream's icon/color and membership rows (no slim-BFF data for
// them). No budget row — upstream has no project-budget field (see
// shared/types.ts).

"use client";

import { useMemo, useState, type ReactNode } from "react";
import Link from "next/link";
import { Target, X } from "lucide-react";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { cn } from "@/lib/utils";
import { handleShellLinkClick, teamsDetailUrl } from "@/lib/shellNav";
import {
  PROJECT_STATUSES,
  type Project,
  type ProjectGoalRef,
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
  /** All company goals, used as the "add a goal" option list (minus those
   * already linked). When omitted the add affordance is hidden and the row
   * shows only the current chips. */
  goals?: ProjectGoalRef[];
  /** Persist the FULL desired set of linked goal ids (PATCH `{ goal_ids }`).
   * Both add and remove send the whole array — replace-semantics matching
   * upstream `updateProjectSchema`, so a stale read can never silently drop an
   * unrelated link. */
  onGoalsChange?: (goalIds: string[]) => void;
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
          style={{ color: "var(--midnight)" }}
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
          color: "var(--midnight)",
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

/** Resolve the project's currently-linked goals as `{ id, title }` refs.
 * Prefers the `goals` refs the payload already carries (titles included);
 * falls back to `goalIds` resolved against the company goals list, then to a
 * truncated id so a link still renders if a title is unavailable. The slim
 * Project type does not model upstream's deprecated single `goalId`, so only
 * the array forms are considered. */
function resolveLinkedGoals(
  project: Project,
  companyGoals: ProjectGoalRef[],
): ProjectGoalRef[] {
  if (project.goals && project.goals.length > 0) return project.goals;
  const ids = project.goalIds ?? [];
  return ids.map((id) => ({
    id,
    title: companyGoals.find((g) => g.id === id)?.title ?? id.slice(0, 8),
  }));
}

/** Inline Goals editor: linked goals render as removable chips and a "+ Goal"
 * Popover offers the unlinked company goals. Add/remove both call back with the
 * FULL desired id set (replace-semantics), so the parent's single PATCH path
 * never has to diff. Mirrors upstream's ProjectProperties goals block. */
function GoalsEditor({
  linkedGoals,
  availableGoals,
  onChange,
}: {
  linkedGoals: ProjectGoalRef[];
  availableGoals: ProjectGoalRef[];
  onChange?: (goalIds: string[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");

  const linkedIds = linkedGoals.map((g) => g.id);

  const visible = query.trim()
    ? availableGoals.filter((g) =>
        g.title.toLowerCase().includes(query.toLowerCase().trim()),
      )
    : availableGoals;

  const addGoal = (goalId: string) => {
    if (!onChange || linkedIds.includes(goalId)) return;
    onChange([...linkedIds, goalId]);
    setOpen(false);
    setQuery("");
  };

  const removeGoal = (goalId: string) => {
    if (!onChange) return;
    onChange(linkedIds.filter((id) => id !== goalId));
  };

  if (linkedGoals.length === 0 && !onChange) {
    return (
      <span data-empty="true" style={EMPTY_DASH_STYLE}>
        —
      </span>
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {linkedGoals.map((goal) => (
        <span
          key={goal.id}
          className="inline-flex items-center gap-1 rounded-sm px-1.5 py-0.5 text-xs"
          style={{
            background: "var(--cream-deep)",
            border: "1px solid var(--edge)",
          }}
        >
          <Link
            href={teamsDetailUrl("goal", goal.id)}
            prefetch={false}
            onClick={(e) => handleShellLinkClick(e, teamsDetailUrl("goal", goal.id))}
            className="min-w-0 truncate hover:underline"
            style={{ color: "var(--midnight)" }}
          >
            {goal.title}
          </Link>
          {onChange && (
            <button
              type="button"
              onClick={() => removeGoal(goal.id)}
              aria-label={`Remove goal ${goal.title}`}
              className="shrink-0 hover:opacity-70"
              style={{ color: "var(--midnight-muted)" }}
            >
              <X size={12} strokeWidth={1.75} />
            </button>
          )}
        </span>
      ))}
      {onChange && (
        <Popover open={open} onOpenChange={setOpen}>
          <PopoverTrigger asChild>
            <button
              type="button"
              disabled={availableGoals.length === 0}
              className={cn(
                "inline-flex items-center gap-1 rounded-sm px-1.5 py-0.5 text-xs hover:opacity-80 disabled:opacity-50 disabled:cursor-not-allowed",
              )}
              style={{ color: "var(--midnight-soft)", border: "1px solid var(--edge)" }}
            >
              <Target size={12} strokeWidth={1.75} />
              Goal
            </button>
          </PopoverTrigger>
          <PopoverContent
            align="start"
            className="w-64 p-1"
            style={{
              background: "var(--cream)",
              border: "1px solid var(--edge)",
              color: "var(--midnight)",
            }}
          >
            <input
              autoFocus
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search goals..."
              className="mb-1 w-full bg-transparent px-2 py-1.5 text-xs"
              style={{ borderBottom: "1px solid var(--edge)" }}
            />
            <ul className="max-h-48 overflow-y-auto" role="listbox">
              {visible.map((goal) => (
                <li key={goal.id} role="option" aria-selected={false}>
                  <button
                    type="button"
                    onClick={() => addGoal(goal.id)}
                    className="w-full rounded-md px-2 py-1.5 text-left text-ui hover:bg-[var(--cream-deep)]"
                  >
                    {goal.title}
                  </button>
                </li>
              ))}
              {visible.length === 0 && (
                <li className="px-2 py-2 text-xs" style={{ color: "var(--midnight-muted)" }}>
                  {availableGoals.length === 0 ? "All goals linked." : "No matches"}
                </li>
              )}
            </ul>
          </PopoverContent>
        </Popover>
      )}
    </div>
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
  // The Goals row holds wrapping chips + an add control, so it opts out of the
  // single-line `truncate` the summary rows use (truncate's white-space:nowrap
  // would otherwise force the chips onto one clipped line).
  wrap = false,
}: {
  label: string;
  children: ReactNode;
  wrap?: boolean;
}) {
  return (
    <div
      className="grid grid-cols-[100px_1fr] gap-3 items-baseline py-2 text-sm"
      style={{
        color: "var(--midnight-soft)",
        borderBottom: "1px solid var(--edge)",
      }}
    >
      <dt className="section-label" style={{ marginTop: 0, marginBottom: 0 }}>
        <b>{label}</b>
      </dt>
      <dd className={cn("min-w-0", !wrap && "truncate")}>{children}</dd>
    </div>
  );
}

const EMPTY_DASH_STYLE = { color: "var(--midnight-muted)" } as const;

export function ProjectProperties({
  project,
  issueCount,
  onStatusChange,
  goals,
  onGoalsChange,
  className,
}: ProjectPropertiesProps) {
  const companyGoals = useMemo(() => goals ?? [], [goals]);
  const linkedGoals = useMemo(
    () => resolveLinkedGoals(project, companyGoals),
    [project, companyGoals],
  );
  const availableGoals = useMemo(() => {
    const linkedIds = new Set(linkedGoals.map((g) => g.id));
    return companyGoals.filter((g) => !linkedIds.has(g.id));
  }, [companyGoals, linkedGoals]);

  return (
    <dl className={cn("flex flex-col", className)}>
      <PropertyRow label="Status">
        <StatusPicker
          status={asProjectStatus(project.status)}
          onChange={onStatusChange}
        />
      </PropertyRow>
      <PropertyRow label="Goals" wrap>
        <GoalsEditor
          linkedGoals={linkedGoals}
          availableGoals={availableGoals}
          onChange={onGoalsChange}
        />
      </PropertyRow>
      <PropertyRow label="Issues">
        <span className="tabular-nums" style={{ color: "var(--midnight)" }}>
          {issueCount}
        </span>
      </PropertyRow>
      <PropertyRow label="Created">
        {project.createdAt ? (
          <time dateTime={project.createdAt} style={{ color: "var(--midnight-muted)" }}>
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
          <time dateTime={project.updatedAt} style={{ color: "var(--midnight-muted)" }}>
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
