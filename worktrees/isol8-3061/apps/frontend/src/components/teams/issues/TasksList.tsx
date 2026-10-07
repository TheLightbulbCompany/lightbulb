// apps/frontend/src/components/teams/issues/TasksList.tsx
//
// Tasks list + board surface — the full-fat replacement for the old thin
// 3-column issues table. Modelled on upstream Paperclip's IssuesList
// (paperclip/ui/src/components/IssuesList.tsx) (MIT, (c) 2025 Paperclip AI),
// reskinned onto the editorial cream/forest palette and trimmed to the data
// our slim Teams BFF actually ships.
//
// v1 scope (see PR notes):
//   - List view: nested/tree rows (parent → expandable sub-tasks), a clickable
//     status circle (4+ states, opens the status menu), the ISO-N identifier,
//     title, and a relative "Updated …" timestamp. Row click opens the detail.
//   - Board view: reuses the existing inbox KanbanBoard verbatim.
//   - List/board view-mode toggle (board is the default; the choice persists
//     per browser — see `tasksViewMode.ts`), plus list-only Sort, Group, and
//     tree-nesting toggles.
//   - Search + New Task live in the panel masthead/toolbar (passed down).
//
// Deferred vs upstream: workspace/label/assignee column picker, board density
// controls, infinite-scroll windowing, workflow checklist affordances, and the
// sub-issue progress strip. Noted as clean follow-ups — none block v1.

"use client";

import { useCallback, useMemo, useState } from "react";
import Link from "next/link";
import {
  ArrowUpDown,
  Check,
  ChevronRight,
  Columns3,
  EyeOff,
  Layers,
  List,
  ListChecks,
  ListTree,
  MoreHorizontal,
} from "lucide-react";
import { cn } from "@/lib/utils";
import {
  setTasksViewMode,
  useTasksViewMode,
  type TasksViewMode,
} from "@/components/teams/issues/tasksViewMode";
import { teamsDetailUrl } from "@/lib/shellNav";
import { useInlineTeamsNav } from "@/components/chat/InlineTeamsNav";
import { buildSubIssueProgressSummary } from "@/components/teams/shared/lib/subIssues";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { StatusIcon } from "@/components/teams/shared/components/StatusIcon";
import { PriorityIcon } from "@/components/teams/shared/components/PriorityIcon";
import { Identity } from "@/components/teams/shared/components/Identity";
import { LiveRunDot } from "@/components/teams/shared/components/LiveRunDot";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { PanelEmptyState } from "@/components/teams/shell";
import { IssueFiltersPopover } from "@/components/teams/inbox/IssueFiltersPopover";
import { hideIssueWithUndo } from "./lib/hideIssue";
import {
  KanbanBoard,
  type KanbanBoardProps,
} from "@/components/teams/inbox/KanbanBoard";
import {
  applyIssueFilters,
  countActiveIssueFilters,
  defaultIssueFilterState,
  type IssueFilterState,
} from "@/components/teams/shared/lib/issueFilters";
import type {
  Goal,
  IssueBlockerAttention,
  Issue,
} from "@/components/teams/shared/types";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

// Shared chip styling for label pills, lifted from IssueProperties' Labels
// row so the trailing label chips read identically to the detail rail.
const LABEL_CHIP_STYLE = {
  background: "var(--cream-deep)",
  color: "var(--midnight-soft)",
  border: "1px solid var(--edge)",
} as const;

export type { TasksViewMode } from "@/components/teams/issues/tasksViewMode";
export type TasksSortField =
  | "status"
  | "title"
  | "updated"
  | "created"
  | "priority";
export type TasksSortDir = "asc" | "desc";
export type TasksGroupBy =
  | "none"
  | "status"
  | "assignee"
  | "priority"
  | "project"
  | "parent"
  | "goal";

type Agent = NonNullable<KanbanBoardProps["agents"]>[number];

export interface TasksListProps {
  issues: Issue[];
  agents?: Agent[];
  /** Company goals, so a goal group heading reads as a TITLE, not an id. */
  goals?: Goal[];
  /**
   * Grouping this mount opens on — a `?groupBy=` deep link, or the Goals /
   * Projects keys that now alias here. Already validated by the router.
   * Absent means the plain Tasks page: no grouping.
   */
  initialGroupBy?: TasksGroupBy;
  /**
   * Fired whenever the LIVE grouping changes — the deep link on mount, and
   * every pick from the Group select after it. The panel above needs this
   * because "New goal" / "New project" belong to the grouping the owner is
   * actually looking at, not the one the URL happened to open on.
   */
  onGroupByChange?: (groupBy: TasksGroupBy) => void;
  liveIssueIds?: Set<string>;
  /** Signed-in user id; passed to the filters popover for the "Me" assignee. */
  currentUserId?: string | null;
  /**
   * True when the parent panel's search box is narrowing the `issues` it
   * passes. Lets the empty state distinguish "search/filter emptied the list"
   * (filtered-empty copy) from "the company genuinely has no tasks" (the
   * create-prompt). The popover's own active filters are tracked internally.
   */
  searchActive?: boolean;
  isLoading?: boolean;
  /** Patches an issue (status / assignee / …). Routed to the BFF PATCH. */
  // Returns the in-flight PATCH so callers that need to sequence on it can —
  // Hide waits for the write to land before offering its Undo toast. Callers
  // that only fire-and-forget (status circle, board drag) ignore it.
  onUpdateIssue: (id: string, data: Record<string, unknown>) => void | Promise<unknown>;
  /** Opens the New Task dialog (rendered by the parent panel). */
  onNewTask: () => void;
}

/* ── tree helpers ──────────────────────────────────────────────────────────
 *
 * Our slim type carries `parentId`; build the same root/child structure
 * upstream's `buildIssueTree` does. Only parents that are themselves present
 * in the visible set get to nest their children — an orphaned child (parent
 * filtered out) is promoted to a root so it never disappears.
 */

interface IssueTree {
  roots: Issue[];
  childMap: Map<string, Issue[]>;
}

function buildIssueTree(issues: Issue[]): IssueTree {
  const byId = new Set(issues.map((i) => i.id));
  const childMap = new Map<string, Issue[]>();
  const roots: Issue[] = [];
  for (const issue of issues) {
    const parentId = issue.parentId ?? null;
    if (parentId && byId.has(parentId)) {
      const siblings = childMap.get(parentId) ?? [];
      siblings.push(issue);
      childMap.set(parentId, siblings);
    } else {
      roots.push(issue);
    }
  }
  return { roots, childMap };
}

function countDescendants(id: string, childMap: Map<string, Issue[]>): number {
  const children = childMap.get(id) ?? [];
  let total = children.length;
  for (const child of children) total += countDescendants(child.id, childMap);
  return total;
}

/* ── sort / group ──────────────────────────────────────────────────────── */

// Workflow order so a "Status" sort reads backlog → done left-to-right.
const STATUS_ORDER: string[] = [
  "backlog",
  "todo",
  "in_progress",
  "in_review",
  "pending",
  "review",
  "blocked",
  "done",
  "duplicate",
  "won_t_do",
  "cancelled",
  "open",
  "closed",
];

function statusRank(status: string): number {
  const idx = STATUS_ORDER.indexOf(status);
  return idx === -1 ? STATUS_ORDER.length : idx;
}

// Priority weight so a "Priority" sort/group reads critical → low. Issues with
// no priority sort last (and bucket into their own "No priority" group).
const PRIORITY_ORDER: string[] = ["critical", "high", "medium", "low"];

function priorityRank(priority: string | null | undefined): number {
  if (!priority) return PRIORITY_ORDER.length;
  const idx = PRIORITY_ORDER.indexOf(priority);
  return idx === -1 ? PRIORITY_ORDER.length : idx;
}

function issueTimestamp(issue: Issue, field: "updated" | "created"): number {
  const raw =
    field === "created"
      ? issue.createdAt
      : (issue.lastActivityAt ?? issue.updatedAt ?? issue.createdAt);
  return raw ? new Date(raw).getTime() : 0;
}

function sortIssues(
  issues: Issue[],
  field: TasksSortField,
  dir: TasksSortDir,
): Issue[] {
  const mul = dir === "asc" ? 1 : -1;
  return [...issues].sort((a, b) => {
    switch (field) {
      case "status":
        return mul * (statusRank(a.status) - statusRank(b.status));
      case "priority":
        return mul * (priorityRank(a.priority) - priorityRank(b.priority));
      case "title":
        return mul * a.title.localeCompare(b.title);
      case "created":
        return mul * (issueTimestamp(a, "created") - issueTimestamp(b, "created"));
      case "updated":
      default:
        return mul * (issueTimestamp(a, "updated") - issueTimestamp(b, "updated"));
    }
  });
}

const SORT_OPTIONS: ReadonlyArray<readonly [TasksSortField, string]> = [
  ["updated", "Updated"],
  ["created", "Created"],
  ["status", "Status"],
  ["priority", "Priority"],
  ["title", "Title"],
];

// Goals and Projects were their own tabs until they became groupings of this
// one. `panelRegistry.TASKS_GROUP_BY` is the deep-link fence over these same
// values; TasksList.groupBy.test.tsx pins the two lists together.
export const GROUP_OPTIONS: ReadonlyArray<readonly [TasksGroupBy, string]> = [
  ["none", "None"],
  ["status", "Status"],
  ["assignee", "Assignee"],
  ["priority", "Priority"],
  ["project", "Project"],
  ["goal", "Goal"],
  ["parent", "Parent"],
];

function statusLabel(status: string): string {
  return status.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

interface TasksGroup {
  key: string;
  label: string | null;
  items: Issue[];
  /** Set on a goal group: the heading links to that goal's detail, so its
   *  description, status and owner stay reachable now that Goals is not a
   *  tab. Absent on the "No goal" bucket, which has nothing to open. */
  goalId?: string;
  /** One extra fact about the group — "2 of 5 done" for a goal. */
  meta?: string;
}

interface GroupContext {
  agentName: (id: string | null | undefined) => string | null;
  /** Resolves a parent issue's identifier/title for the "Parent" grouping. */
  issueLabel: (id: string) => string | null;
  /** Resolves a goal's TITLE for the "Goal" grouping — never its raw id. */
  goalTitle: (id: string) => string | null;
  /**
   * True when a search or a filter has already narrowed the issues being
   * grouped. A goal's "N of M" is then a count of what is SHOWN, not of the
   * goal, and must say so.
   */
  isFiltered: boolean;
}

// Sentinel keys for the "no value" bucket of each grouping. Kept distinct so
// the empty bucket sorts last and renders a readable header per dimension.
const UNASSIGNED_KEY = "__unassigned";
const NO_PRIORITY_KEY = "__no_priority";
const NO_PROJECT_KEY = "__no_project";
const NO_PARENT_KEY = "__no_parent";
const NO_GOAL_KEY = "__no_goal";

function groupKeyFor(issue: Issue, groupBy: TasksGroupBy): string {
  switch (groupBy) {
    case "status":
      return issue.status;
    case "assignee":
      return issue.assigneeAgentId ?? UNASSIGNED_KEY;
    case "priority":
      return issue.priority ?? NO_PRIORITY_KEY;
    case "project":
      return issue.project?.id ?? NO_PROJECT_KEY;
    case "parent":
      return issue.parentId ?? NO_PARENT_KEY;
    case "goal":
      return issue.goalId ?? NO_GOAL_KEY;
    default:
      return "__all";
  }
}

function groupIssues(
  issues: Issue[],
  groupBy: TasksGroupBy,
  ctx: GroupContext,
): TasksGroup[] {
  if (groupBy === "none") {
    return [{ key: "__all", label: null, items: issues }];
  }
  const buckets = new Map<string, Issue[]>();
  // Cache the project name off the first issue in each project bucket — the
  // slim Issue carries the denormalized `project` object so no roster lookup
  // is needed.
  const projectNames = new Map<string, string>();
  for (const issue of issues) {
    const key = groupKeyFor(issue, groupBy);
    if (groupBy === "project" && issue.project) {
      projectNames.set(issue.project.id, issue.project.name);
    }
    const list = buckets.get(key) ?? [];
    list.push(issue);
    buckets.set(key, list);
  }
  const labelFor = (key: string): string => {
    if (groupBy === "status") return statusLabel(key);
    if (groupBy === "priority") {
      if (key === NO_PRIORITY_KEY) return "No priority";
      return statusLabel(key);
    }
    if (groupBy === "project") {
      if (key === NO_PROJECT_KEY) return "No project";
      return projectNames.get(key) ?? key.slice(0, 8);
    }
    if (groupBy === "parent") {
      if (key === NO_PARENT_KEY) return "No parent";
      return ctx.issueLabel(key) ?? key.slice(0, 8);
    }
    if (groupBy === "goal") {
      if (key === NO_GOAL_KEY) return "No goal";
      // Never the raw `goal_…` id: an unresolvable goal says so instead.
      return ctx.goalTitle(key) ?? "Unknown goal";
    }
    // assignee
    if (key === UNASSIGNED_KEY) return "Unassigned";
    return ctx.agentName(key) ?? key.slice(0, 8);
  };
  const emptyKey =
    groupBy === "priority"
      ? NO_PRIORITY_KEY
      : groupBy === "project"
        ? NO_PROJECT_KEY
        : groupBy === "parent"
          ? NO_PARENT_KEY
          : groupBy === "goal"
            ? NO_GOAL_KEY
            : UNASSIGNED_KEY;
  const keys = [...buckets.keys()];
  if (groupBy === "status") {
    keys.sort((a, b) => statusRank(a) - statusRank(b));
  } else if (groupBy === "priority") {
    keys.sort((a, b) => priorityRank(a) - priorityRank(b));
  } else {
    keys.sort((a, b) => {
      if (a === emptyKey) return 1;
      if (b === emptyKey) return -1;
      return labelFor(a).localeCompare(labelFor(b));
    });
  }
  return keys.map((key) => {
    const items = buckets.get(key)!;
    if (groupBy !== "goal") return { key, label: labelFor(key), items };
    // "N of M done" off the SAME progress helper the sub-issue strip uses, so
    // there is one definition of done on this surface (it drops cancelled work
    // from the denominator rather than counting it as outstanding).
    // `settledCount`, not `doneCount`: a task that was closed or marked
    // won't-do is finished, and counting only `done` leaves a goal stuck
    // below its own total forever.
    const { settledCount, totalCount } = buildSubIssueProgressSummary(items);
    // These counts are computed from `items`, which the search box and the
    // "See her work" agent filter have already narrowed — so the label says
    // "shown" rather than implying it describes the whole goal.
    const scoped = ctx.isFiltered;
    return {
      key,
      label: labelFor(key),
      items,
      // An id with no title in the roster is an orphan: showing "Unknown goal"
      // is fair, linking to a detail page for a goal that is not there is not.
      goalId: key === NO_GOAL_KEY || !ctx.goalTitle(key) ? undefined : key,
      meta: scoped
        ? `${settledCount} of ${totalCount} shown done`
        : `${settledCount} of ${totalCount} done`,
    };
  });
}

/* ── view-mode toggle ──────────────────────────────────────────────────── */

function ViewModeToggle({
  viewMode,
  onChange,
}: {
  viewMode: TasksViewMode;
  onChange: (mode: TasksViewMode) => void;
}) {
  const buttonClass = (active: boolean) =>
    cn(
      "inline-flex h-7 w-7 items-center justify-center transition-colors",
      active
        ? "bg-[var(--midnight)] text-[var(--cream)]"
        : "text-[var(--midnight-muted)] hover:text-[var(--midnight)]",
    );
  return (
    <div className="flex items-center overflow-hidden rounded-md border border-[var(--edge)]">
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            className={buttonClass(viewMode === "list")}
            onClick={() => onChange("list")}
            aria-pressed={viewMode === "list"}
            aria-label="List view"
          >
            <List size={16} />
          </button>
        </TooltipTrigger>
        <TooltipContent>List view</TooltipContent>
      </Tooltip>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            className={buttonClass(viewMode === "board")}
            onClick={() => onChange("board")}
            aria-pressed={viewMode === "board"}
            aria-label="Board view"
          >
            <Columns3 size={16} />
          </button>
        </TooltipTrigger>
        <TooltipContent>Board view</TooltipContent>
      </Tooltip>
    </div>
  );
}

function ToolbarIconButton({
  active,
  title,
  onClick,
  children,
}: {
  active?: boolean;
  title: string;
  onClick?: () => void;
  children: React.ReactNode;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          onClick={onClick}
          aria-label={title}
          className={cn(
            "inline-flex h-7 w-7 items-center justify-center rounded-md border border-[var(--edge)] transition-colors",
            active
              ? "bg-[var(--cream-deeper)] text-[var(--midnight)]"
              : "text-[var(--midnight-muted)] hover:text-[var(--midnight)] hover:bg-[var(--cream-deep)]",
          )}
        >
          {children}
        </button>
      </TooltipTrigger>
      <TooltipContent>{title}</TooltipContent>
    </Tooltip>
  );
}

function MenuPopover<T extends string>({
  icon,
  title,
  active,
  options,
  selected,
  trailing,
  onSelect,
}: {
  icon: React.ReactNode;
  title: string;
  active?: boolean;
  options: ReadonlyArray<readonly [T, string]>;
  selected: T;
  trailing?: (value: T) => React.ReactNode;
  onSelect: (value: T) => void;
}) {
  return (
    <Popover>
      <Tooltip>
        <TooltipTrigger asChild>
          <PopoverTrigger asChild>
            <button
              type="button"
              aria-label={title}
              className={cn(
                "inline-flex h-7 w-7 items-center justify-center rounded-md border border-[var(--edge)] transition-colors",
                active
                  ? "bg-[var(--cream-deeper)] text-[var(--midnight)]"
                  : "text-[var(--midnight-muted)] hover:text-[var(--midnight)] hover:bg-[var(--cream-deep)]",
              )}
            >
              {icon}
            </button>
          </PopoverTrigger>
        </TooltipTrigger>
        <TooltipContent>{title}</TooltipContent>
      </Tooltip>
      <PopoverContent align="end" className="w-44 p-1">
        {options.map(([value, label]) => (
          <button
            key={value}
            type="button"
            onClick={() => onSelect(value)}
            className={cn(
              "flex w-full items-center justify-between rounded-md px-2 py-1.5 text-ui transition-colors",
              value === selected
                ? "bg-[var(--cream-deeper)] text-[var(--midnight)]"
                : "text-[var(--midnight-soft)] hover:bg-[var(--cream-deep)]",
            )}
          >
            <span>{label}</span>
            {value === selected
              ? (trailing?.(value) ?? <Check size={16} />)
              : null}
          </button>
        ))}
      </PopoverContent>
    </Popover>
  );
}

/* ── group header ──────────────────────────────────────────────────────── */

function GroupHeader({
  label,
  count,
  goalId,
  meta,
}: {
  label: string;
  count: number;
  /** Set on a goal group: the heading becomes a link to that goal's detail. */
  goalId?: string;
  /** One extra fact about the group — "2 of 5 done" for a goal. */
  meta?: string;
}) {
  // Same plain-left-click handoff TaskRow uses: the href is the canonical
  // in-shell URL so new-tab still works, and the nav provider takes a plain
  // click so the swap stays client-side.
  const nav = useInlineTeamsNav();
  const heading = <span className="label-caps text-[var(--midnight-muted)]">{label}</span>;
  return (
    <div className="flex items-center gap-2 px-1 pb-1.5 pt-3">
      {goalId ? (
        <Link
          href={teamsDetailUrl("goal", goalId)}
          prefetch={false}
          onClick={(e) => {
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
            nav.openDetail("goal-detail", goalId);
          }}
          className="no-underline hover:underline"
        >
          {heading}
        </Link>
      ) : (
        heading
      )}
      {/* A goal heading already states its shape as "N of M done"; printing a
          bare item count beside it put two unlabelled numbers next to each
          other that disagree by design (the meta drops cancelled work). */}
      {!meta && (
        <span className="text-caption tabular-nums text-[var(--midnight-muted)]">{count}</span>
      )}
      {meta && (
        <span className="text-caption tabular-nums text-[var(--midnight-muted)]">{meta}</span>
      )}
    </div>
  );
}

/* ── row ───────────────────────────────────────────────────────────────── */

function blockerAttentionOf(
  issue: Issue,
): IssueBlockerAttention | null {
  // The slim Issue type allows `blockerAttention` to be a bare bool from
  // legacy rows. StatusIcon only consumes the rich object shape, so narrow
  // here and pass null for anything that isn't the full payload.
  const value = issue.blockerAttention;
  if (value && typeof value === "object") return value;
  return null;
}

interface TaskRowProps {
  issue: Issue;
  depth: number;
  hasChildren: boolean;
  expanded: boolean;
  descendantCount: number;
  isLive: boolean;
  /** Resolves the assignee agent's display name (null = unassigned/unknown). */
  assigneeName: string | null;
  onToggleExpand: () => void;
  // Returns the in-flight PATCH so callers that need to sequence on it can —
  // Hide waits for the write to land before offering its Undo toast. Callers
  // that only fire-and-forget (status circle, board drag) ignore it.
  onUpdateIssue: (id: string, data: Record<string, unknown>) => void | Promise<unknown>;
}

function TaskRow({
  issue,
  depth,
  hasChildren,
  expanded,
  descendantCount,
  isLive,
  assigneeName,
  onToggleExpand,
  onUpdateIssue,
}: TaskRowProps) {
  const nav = useInlineTeamsNav();
  const identifier = issue.identifier ?? issue.id.slice(0, 8);
  const labels = issue.labels ?? [];
  const updatedLabel = (() => {
    const ts = issue.lastActivityAt ?? issue.updatedAt ?? issue.createdAt;
    return ts ? `Updated ${timeAgo(ts)}` : "Updated recently";
  })();

  const handleClick = (e: React.MouseEvent) => {
    // Mirror KanbanCard: keep native <a> behaviour for modified clicks
    // (new tab/window); route a plain left-click to the in-shell detail panel
    // when the nav provider is present. Without a provider the href — the
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
  };

  // The whole row is a Link; the status circle + chevron stop propagation so
  // clicking them never navigates (matches upstream IssueRow's inline actions).
  const stop = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
  };

  return (
    <Link
      href={teamsDetailUrl("issue", issue.id)}
      prefetch={false}
      onClick={handleClick}
      draggable={false}
      className="group flex items-center gap-2 border-b border-[var(--edge)] py-2.5 pr-2 text-sm no-underline transition-colors last:border-b-0 hover:bg-[var(--cream-deep)]"
      style={{
        color: "var(--midnight)",
        paddingLeft: depth > 0 ? `${8 + depth * 18}px` : "8px",
      }}
    >
      {/* Expand chevron — only for rows that actually have nested sub-tasks. */}
      {hasChildren ? (
        <button
          type="button"
          onClick={(e) => {
            stop(e);
            onToggleExpand();
          }}
          aria-label={expanded ? "Collapse sub-tasks" : "Expand sub-tasks"}
          aria-expanded={expanded}
          className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-sm text-[var(--midnight-muted)] hover:text-[var(--midnight)]"
        >
          <ChevronRight size={16}
            className={cn(
              "transition-transform",
              expanded && "rotate-90",
            )}
          />
        </button>
      ) : (
        <span className="inline-block w-4 shrink-0" aria-hidden="true" />
      )}

      {/* Clickable status circle — opens the status menu inline. */}
      <span className="inline-flex shrink-0 items-center" onClick={stop}>
        <StatusIcon
          status={issue.status}
          blockerAttention={blockerAttentionOf(issue)}
          onChange={(s) => onUpdateIssue(issue.id, { status: s })}
        />
      </span>

      {/* ISO-N identifier */}
      <span className="shrink-0 font-mono text-xs text-[var(--midnight-muted)]">
        {identifier}
      </span>

      {/* Live indicator — a pulsing forest dot while an agent run is active on
          this task. Mirrors the board (KanbanCard) so list/board read alike. */}
      {isLive ? <LiveRunDot testId="task-row-live-dot" /> : null}

      {/* Title (+ collapsed sub-task count) */}
      <span className="min-w-0 flex-1 truncate">
        {issue.title}
        {hasChildren && !expanded && descendantCount > 0 ? (
          <span className="ml-1.5 text-xs text-[var(--midnight-muted)]">
            ({descendantCount} sub-task{descendantCount !== 1 ? "s" : ""})
          </span>
        ) : null}
      </span>

      {/* Trailing metadata — compact, calm: labels (≤2 + overflow), priority,
          assignee chip. Hidden on narrow widths so the row stays squint-legible
          (the timestamp + status circle are the load-bearing signals). */}
      {labels.length > 0 ? (
        <span className="hidden shrink-0 items-center gap-1 md:flex">
          {labels.slice(0, 2).map((label) => (
            <span
              key={label.id}
              className="max-w-[8rem] truncate rounded-sm px-1.5 py-0.5 text-caption"
              style={LABEL_CHIP_STYLE}
            >
              {label.name}
            </span>
          ))}
          {labels.length > 2 ? (
            <span className="text-caption text-[var(--midnight-muted)]">
              +{labels.length - 2}
            </span>
          ) : null}
        </span>
      ) : null}

      {issue.priority ? (
        <span className="hidden shrink-0 sm:inline-flex">
          <PriorityIcon priority={issue.priority} />
        </span>
      ) : null}

      {/* Assignee chip — initials avatar + name when assigned to an agent,
          a muted "Unassigned" otherwise. */}
      {issue.assigneeAgentId ? (
        <span className="hidden min-w-0 max-w-[9rem] shrink-0 lg:inline-flex">
          <Identity
            name={assigneeName ?? issue.assigneeAgentId.slice(0, 8)}
            size="sm"
            className="min-w-0"
          />
        </span>
      ) : (
        <span className="hidden shrink-0 text-xs text-[var(--midnight-muted)] lg:inline">
          Unassigned
        </span>
      )}

      {/* Relative activity timestamp */}
      <span className="shrink-0 text-xs text-[var(--midnight-muted)]">{updatedLabel}</span>

      {/* Row-level overflow menu — revealed on row hover. Like the status
          circle, every handler stops propagation so opening the menu or
          picking Hide never triggers the row's Link navigation. */}
      <span
        className="shrink-0 opacity-0 transition-opacity duration-(--dur-fast) ease-out group-hover:opacity-100 group-focus-within:opacity-100 focus-visible:opacity-100"
        onClick={stop}
      >
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              onClick={stop}
              aria-label="Task actions"
              className="inline-flex h-6 w-6 items-center justify-center rounded-sm text-[var(--midnight-muted)] transition-colors hover:bg-[var(--cream)] hover:text-[var(--midnight)]"
            >
              <MoreHorizontal size={16} />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem
              onSelect={(e) => {
                e.preventDefault();
                void hideIssueWithUndo(issue.id, onUpdateIssue);
              }}
            >
              <EyeOff size={16} />
              Hide
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </span>
    </Link>
  );
}

/* ── component ─────────────────────────────────────────────────────────── */

export function TasksList({
  issues,
  agents,
  goals,
  initialGroupBy,
  onGroupByChange,
  liveIssueIds,
  currentUserId,
  searchActive = false,
  isLoading,
  onUpdateIssue,
  onNewTask,
}: TasksListProps) {
  const storedViewMode = useTasksViewMode();
  const [sortField, setSortField] = useState<TasksSortField>("updated");
  const [sortDir, setSortDir] = useState<TasksSortDir>("desc");
  const [groupBy, setGroupByState] = useState<TasksGroupBy>(initialGroupBy ?? "none");
  // One writer, so the panel above can never hold a grouping the list is not
  // showing: every path that changes the grouping goes through this.
  const setGroupBy = useCallback(
    (next: TasksGroupBy) => {
      setGroupByState(next);
      onGroupByChange?.(next);
    },
    [onGroupByChange],
  );
  // A ?groupBy deep link — the old Goals / Projects links — asks for a GROUPED
  // view, and grouping is a list-view affordance. Honour it on this mount
  // without touching the stored board-vs-list preference: the view toggle
  // takes the view back, and the preference is what it always was.
  const [groupingDeepLink, setGroupingDeepLink] = useState(Boolean(initialGroupBy));
  const viewMode = groupingDeepLink ? "list" : storedViewMode;
  // Re-delivery of the panel param on an already-open panel (dockview merges
  // parameters rather than remounting), and the clear a plain Tasks nav sends.
  // Adjust-state-during-render, not an effect — the same pattern IssuesPanel
  // uses for its one-shot.
  const [prevInitialGroupBy, setPrevInitialGroupBy] = useState(initialGroupBy);
  if (initialGroupBy !== prevInitialGroupBy) {
    setPrevInitialGroupBy(initialGroupBy);
    setGroupByState(initialGroupBy ?? "none");
    setGroupingDeepLink(Boolean(initialGroupBy));
  }
  const [nestingEnabled, setNestingEnabled] = useState(true);
  const [filterState, setFilterState] = useState<IssueFilterState>(
    defaultIssueFilterState,
  );
  const [collapsedParents, setCollapsedParents] = useState<Set<string>>(
    () => new Set(),
  );

  const goalTitle = useCallback(
    (id: string): string | null => goals?.find((g) => g.id === id)?.title ?? null,
    [goals],
  );

  const agentName = useCallback(
    (id: string | null | undefined): string | null => {
      if (!id || !agents) return null;
      return agents.find((a) => a.id === id)?.name ?? null;
    },
    [agents],
  );

  // Parent-issue label resolver for the "Group by parent" headers. Built from
  // the in-page issue set so a parent that's itself a row reads as ISO-N Title.
  const issueLabel = useCallback(
    (id: string): string | null => {
      const match = issues.find((i) => i.id === id);
      if (!match) return null;
      const ident = match.identifier ?? match.id.slice(0, 8);
      return `${ident} ${match.title}`.trim();
    },
    [issues],
  );

  const activeFilterCount = useMemo(
    () => countActiveIssueFilters(filterState),
    [filterState],
  );
  const filtersActive = activeFilterCount > 0;

  // Project + label facet options for the popover, derived from the
  // denormalized objects on the in-page issues (the slim BFF doesn't fan out a
  // separate labels endpoint, so the visible issue set is the source of truth).
  const projectFilterOptions = useMemo(() => {
    const map = new Map<string, string>();
    for (const issue of issues) {
      if (issue.project) map.set(issue.project.id, issue.project.name);
    }
    return [...map].map(([id, name]) => ({ id, name }));
  }, [issues]);

  const labelFilterOptions = useMemo(() => {
    const map = new Map<string, { name: string; color: string }>();
    for (const issue of issues) {
      for (const label of issue.labels ?? []) {
        if (!map.has(label.id)) {
          map.set(label.id, { name: label.name, color: label.color ?? "" });
        }
      }
    }
    return [...map].map(([id, { name, color }]) => ({ id, name, color }));
  }, [issues]);

  // Apply the popover filter state CLIENT-SIDE before sort/group — the same
  // shared predicate InboxPage uses, so the two surfaces never drift. Search
  // is applied upstream by the parent panel; this layer is the structured
  // facet filter (status / priority / assignee / labels / project / live).
  const filtered = useMemo(
    () =>
      applyIssueFilters(issues, filterState, {
        currentUserId,
        liveIssueIds,
      }),
    [issues, filterState, currentUserId, liveIssueIds],
  );

  const sorted = useMemo(
    () => sortIssues(filtered, sortField, sortDir),
    [filtered, sortField, sortDir],
  );

  const groups = useMemo(
    () =>
      groupIssues(sorted, groupBy, {
        agentName,
        issueLabel,
        goalTitle,
        isFiltered: filtersActive || searchActive,
      }),
    [sorted, groupBy, agentName, issueLabel, goalTitle, filtersActive, searchActive],
  );

  const toggleParent = useCallback((id: string) => {
    setCollapsedParents((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  // A status sort makes nesting confusing (children would float away from
  // their parent), so tree mode only applies to the default-ish sorts.
  const treeActive = nestingEnabled && groupBy === "none";

  const renderRows = useCallback(
    (items: Issue[]) => {
      const { roots, childMap } = treeActive
        ? buildIssueTree(items)
        : { roots: items, childMap: new Map<string, Issue[]>() };

      const rows: React.ReactNode[] = [];
      const walk = (issue: Issue, depth: number) => {
        const children = childMap.get(issue.id) ?? [];
        const hasChildren = children.length > 0;
        const expanded = !collapsedParents.has(issue.id);
        rows.push(
          <TaskRow
            key={issue.id}
            issue={issue}
            depth={depth}
            hasChildren={hasChildren}
            expanded={expanded}
            descendantCount={
              hasChildren ? countDescendants(issue.id, childMap) : 0
            }
            isLive={liveIssueIds?.has(issue.id) ?? false}
            assigneeName={agentName(issue.assigneeAgentId)}
            onToggleExpand={() => toggleParent(issue.id)}
            onUpdateIssue={onUpdateIssue}
          />,
        );
        if (hasChildren && expanded) {
          for (const child of children) walk(child, depth + 1);
        }
      };
      for (const root of roots) walk(root, 0);
      return rows;
    },
    [
      treeActive,
      collapsedParents,
      toggleParent,
      onUpdateIssue,
      liveIssueIds,
      agentName,
    ],
  );

  return (
    <div className="flex flex-col gap-3">
      {/* Toolbar */}
      <div className="flex items-center justify-end gap-1.5">
        {/* Filters apply to both list AND board, so the popover sits outside
            the list-only view-mode gate. The popover takes Partial<state>
            patches; merge them onto the current state here. */}
        <IssueFiltersPopover
          state={filterState}
          onChange={(patch) =>
            setFilterState((prev) => ({ ...prev, ...patch }))
          }
          activeFilterCount={activeFilterCount}
          agents={agents}
          projects={projectFilterOptions}
          labels={labelFilterOptions}
          currentUserId={currentUserId}
          buttonVariant="outline"
          iconOnly
        />

        <ViewModeToggle
          viewMode={viewMode}
          onChange={(mode) => {
            // An explicit pick beats the deep link and becomes the preference.
            setGroupingDeepLink(false);
            setTasksViewMode(mode);
          }}
        />

        {viewMode === "list" && (
          <>
            <ToolbarIconButton
              active={treeActive}
              title={
                nestingEnabled
                  ? "Disable parent-child nesting"
                  : "Enable parent-child nesting"
              }
              onClick={() => setNestingEnabled((v) => !v)}
            >
              <ListTree size={16} />
            </ToolbarIconButton>

            <MenuPopover<TasksSortField>
              icon={<ArrowUpDown size={16} />}
              title="Sort"
              options={SORT_OPTIONS}
              selected={sortField}
              trailing={() => (
                <span className="text-xs text-[var(--midnight-muted)]">
                  {sortDir === "asc" ? "↑" : "↓"}
                </span>
              )}
              onSelect={(field) => {
                if (field === sortField) {
                  setSortDir((d) => (d === "asc" ? "desc" : "asc"));
                } else {
                  setSortField(field);
                  setSortDir("desc");
                }
              }}
            />

            <MenuPopover<TasksGroupBy>
              icon={<Layers size={16} />}
              title="Group"
              active={groupBy !== "none"}
              options={GROUP_OPTIONS}
              selected={groupBy}
              onSelect={setGroupBy}
            />
          </>
        )}
      </div>

      {/* Body */}
      {isLoading ? (
        <div className="font-editorial-serif text-xl italic text-[var(--midnight-muted)]">
          Loading…
        </div>
      ) : filtered.length === 0 && (filtersActive || searchActive) ? (
        // Tasks exist but the active search/filters yield nothing → distinct
        // filtered-empty state with a Clear escape hatch (no create-prompt,
        // which would be the wrong CTA here). `Clear filters` resets only the
        // popover state we own; the parent's search box is cleared by the
        // user. We still surface it whenever a search is narrowing the list.
        <PanelEmptyState
          icon={ListChecks}
          headline="No tasks match these filters."
          supporting="Try a different filter or clear them to see every task."
          action={
            filtersActive
              ? {
                  label: "Clear filters",
                  onClick: () => setFilterState(defaultIssueFilterState),
                }
              : undefined
          }
        />
      ) : issues.length === 0 ? (
        // The company genuinely has no tasks → create-prompt empty state.
        <PanelEmptyState
          icon={ListChecks}
          headline="No tasks yet."
          supporting="Create a task to track a piece of work — assign it to an agent and follow it through to done."
          action={{ label: "New task", onClick: onNewTask }}
        />
      ) : viewMode === "board" ? (
        // tasks-board-region: inside a dock panel, workspace.css overrides
        // the viewport-height fallback below with a slot-relative height so
        // the board fills the panel (columns scroll vertically, the board
        // scrolls horizontally). The class is inert in the mobile shell.
        <div className="tasks-board-region h-[calc(100vh-18rem)] min-h-[24rem]">
          <KanbanBoard
            issues={sorted}
            agents={agents}
            liveIssueIds={liveIssueIds}
            onUpdateIssue={onUpdateIssue}
          />
        </div>
      ) : (
        <div className="editorial-card overflow-hidden">
          {groups.map((group) => (
            <div key={group.key}>
              {group.label && (
                <GroupHeader
                  label={group.label}
                  count={group.items.length}
                  goalId={group.goalId}
                  meta={group.meta}
                />
              )}
              {renderRows(group.items)}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
