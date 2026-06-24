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
//   - List/board view-mode toggle (persisted in component state), plus list-only
//     Sort, Group, and tree-nesting toggles.
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
import { useInlineTeamsNav } from "@/components/chat/InlineTeamsNav";
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
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { PanelEmptyState } from "@/components/teams/shell";
import {
  KanbanBoard,
  type KanbanBoardProps,
} from "@/components/teams/inbox/KanbanBoard";
import type {
  IssueBlockerAttention,
  Issue,
} from "@/components/teams/shared/types";

export type TasksViewMode = "list" | "board";
export type TasksSortField = "status" | "title" | "updated" | "created";
export type TasksSortDir = "asc" | "desc";
export type TasksGroupBy = "none" | "status" | "assignee";

type Agent = NonNullable<KanbanBoardProps["agents"]>[number];

export interface TasksListProps {
  issues: Issue[];
  agents?: Agent[];
  liveIssueIds?: Set<string>;
  isLoading?: boolean;
  /** Patches an issue (status / assignee / …). Routed to the BFF PATCH. */
  onUpdateIssue: (id: string, data: Record<string, unknown>) => void;
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
  ["title", "Title"],
];

const GROUP_OPTIONS: ReadonlyArray<readonly [TasksGroupBy, string]> = [
  ["none", "None"],
  ["status", "Status"],
  ["assignee", "Assignee"],
];

function statusLabel(status: string): string {
  return status.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

interface TasksGroup {
  key: string;
  label: string | null;
  items: Issue[];
}

function groupIssues(
  issues: Issue[],
  groupBy: TasksGroupBy,
  agentName: (id: string | null | undefined) => string | null,
): TasksGroup[] {
  if (groupBy === "none") {
    return [{ key: "__all", label: null, items: issues }];
  }
  const buckets = new Map<string, Issue[]>();
  for (const issue of issues) {
    const key =
      groupBy === "status"
        ? issue.status
        : (issue.assigneeAgentId ?? "__unassigned");
    const list = buckets.get(key) ?? [];
    list.push(issue);
    buckets.set(key, list);
  }
  const labelFor = (key: string): string => {
    if (groupBy === "status") return statusLabel(key);
    if (key === "__unassigned") return "Unassigned";
    return agentName(key) ?? key.slice(0, 8);
  };
  const keys = [...buckets.keys()];
  if (groupBy === "status") {
    keys.sort((a, b) => statusRank(a) - statusRank(b));
  } else {
    keys.sort((a, b) => {
      if (a === "__unassigned") return 1;
      if (b === "__unassigned") return -1;
      return labelFor(a).localeCompare(labelFor(b));
    });
  }
  return keys.map((key) => ({
    key,
    label: labelFor(key),
    items: buckets.get(key)!,
  }));
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
        ? "bg-[var(--forest)] text-[var(--cream)]"
        : "text-[var(--mute)] hover:text-[var(--ink)]",
    );
  return (
    <div className="flex items-center overflow-hidden rounded-md border border-[var(--edge)]">
      <button
        type="button"
        className={buttonClass(viewMode === "list")}
        onClick={() => onChange("list")}
        aria-pressed={viewMode === "list"}
        title="List view"
        aria-label="List view"
      >
        <List className="h-3.5 w-3.5" />
      </button>
      <button
        type="button"
        className={buttonClass(viewMode === "board")}
        onClick={() => onChange("board")}
        aria-pressed={viewMode === "board"}
        title="Board view"
        aria-label="Board view"
      >
        <Columns3 className="h-3.5 w-3.5" />
      </button>
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
    <button
      type="button"
      onClick={onClick}
      title={title}
      aria-label={title}
      className={cn(
        "inline-flex h-7 w-7 items-center justify-center rounded-md border border-[var(--edge)] transition-colors",
        active
          ? "bg-[var(--forest-tint)] text-[var(--ink)]"
          : "text-[var(--mute)] hover:text-[var(--ink)] hover:bg-[var(--cream-deeper)]",
      )}
    >
      {children}
    </button>
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
      <PopoverTrigger asChild>
        <button
          type="button"
          title={title}
          aria-label={title}
          className={cn(
            "inline-flex h-7 w-7 items-center justify-center rounded-md border border-[var(--edge)] transition-colors",
            active
              ? "bg-[var(--forest-tint)] text-[var(--ink)]"
              : "text-[var(--mute)] hover:text-[var(--ink)] hover:bg-[var(--cream-deeper)]",
          )}
        >
          {icon}
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-44 p-1">
        {options.map(([value, label]) => (
          <button
            key={value}
            type="button"
            onClick={() => onSelect(value)}
            className={cn(
              "flex w-full items-center justify-between rounded-sm px-2 py-1.5 text-sm transition-colors",
              value === selected
                ? "bg-[var(--cream-deeper)] text-[var(--ink)]"
                : "text-[var(--ink-soft)] hover:bg-[var(--cream-deeper)]",
            )}
          >
            <span>{label}</span>
            {value === selected
              ? (trailing?.(value) ?? <Check className="h-3.5 w-3.5" />)
              : null}
          </button>
        ))}
      </PopoverContent>
    </Popover>
  );
}

/* ── group header ──────────────────────────────────────────────────────── */

function GroupHeader({ label, count }: { label: string; count: number }) {
  return (
    <div className="flex items-center gap-2 px-1 pb-1.5 pt-3">
      <span className="text-[11px] font-semibold uppercase tracking-[0.1em] text-[var(--mute)]">
        {label}
      </span>
      <span className="text-[11px] tabular-nums text-[var(--mute-soft)]">
        {count}
      </span>
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
  onToggleExpand: () => void;
  onUpdateIssue: (id: string, data: Record<string, unknown>) => void;
}

function TaskRow({
  issue,
  depth,
  hasChildren,
  expanded,
  descendantCount,
  onToggleExpand,
  onUpdateIssue,
}: TaskRowProps) {
  const nav = useInlineTeamsNav();
  const identifier = issue.identifier ?? issue.id.slice(0, 8);
  const updatedLabel = (() => {
    const ts = issue.lastActivityAt ?? issue.updatedAt ?? issue.createdAt;
    return ts ? `Updated ${timeAgo(ts)}` : "Updated recently";
  })();

  const handleClick = (e: React.MouseEvent) => {
    // Mirror KanbanCard: keep native <a> behaviour for modified clicks
    // (new tab/window) and standalone /teams (no nav provider); route a plain
    // left-click to the in-shell detail panel when the provider is present.
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
      href={`/teams/issues/${issue.id}`}
      onClick={handleClick}
      draggable={false}
      className="group flex items-center gap-2 border-b border-[var(--edge)] py-2.5 pr-2 text-sm no-underline transition-colors last:border-b-0 hover:bg-[var(--cream-deeper)]"
      style={{
        color: "var(--ink)",
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
          className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded text-[var(--mute)] hover:text-[var(--ink)]"
        >
          <ChevronRight
            className={cn(
              "h-3.5 w-3.5 transition-transform",
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
      <span className="shrink-0 font-mono text-xs text-[var(--mute)]">
        {identifier}
      </span>

      {/* Title (+ collapsed sub-task count) */}
      <span className="min-w-0 flex-1 truncate">
        {issue.title}
        {hasChildren && !expanded && descendantCount > 0 ? (
          <span className="ml-1.5 text-xs text-[var(--mute)]">
            ({descendantCount} sub-task{descendantCount !== 1 ? "s" : ""})
          </span>
        ) : null}
      </span>

      {/* Relative activity timestamp */}
      <span className="shrink-0 text-xs text-[var(--mute)]">{updatedLabel}</span>

      {/* Row-level overflow menu — revealed on row hover. Like the status
          circle, every handler stops propagation so opening the menu or
          picking Hide never triggers the row's Link navigation. */}
      <span
        className="shrink-0 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100"
        onClick={stop}
      >
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              onClick={stop}
              aria-label="Task actions"
              className="inline-flex h-6 w-6 items-center justify-center rounded text-[var(--mute)] transition-colors hover:bg-[var(--cream)] hover:text-[var(--ink)]"
            >
              <MoreHorizontal className="h-3.5 w-3.5" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem
              onSelect={(e) => {
                e.preventDefault();
                onUpdateIssue(issue.id, {
                  hiddenAt: new Date().toISOString(),
                });
              }}
            >
              <EyeOff className="h-4 w-4" />
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
  liveIssueIds,
  isLoading,
  onUpdateIssue,
  onNewTask,
}: TasksListProps) {
  const [viewMode, setViewMode] = useState<TasksViewMode>("list");
  const [sortField, setSortField] = useState<TasksSortField>("updated");
  const [sortDir, setSortDir] = useState<TasksSortDir>("desc");
  const [groupBy, setGroupBy] = useState<TasksGroupBy>("none");
  const [nestingEnabled, setNestingEnabled] = useState(true);
  const [collapsedParents, setCollapsedParents] = useState<Set<string>>(
    () => new Set(),
  );

  const agentName = useCallback(
    (id: string | null | undefined): string | null => {
      if (!id || !agents) return null;
      return agents.find((a) => a.id === id)?.name ?? null;
    },
    [agents],
  );

  const sorted = useMemo(
    () => sortIssues(issues, sortField, sortDir),
    [issues, sortField, sortDir],
  );

  const groups = useMemo(
    () => groupIssues(sorted, groupBy, agentName),
    [sorted, groupBy, agentName],
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
    [treeActive, collapsedParents, toggleParent, onUpdateIssue],
  );

  return (
    <div className="flex flex-col gap-3">
      {/* Toolbar */}
      <div className="flex items-center justify-end gap-1.5">
        <ViewModeToggle viewMode={viewMode} onChange={setViewMode} />

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
              <ListTree className="h-3.5 w-3.5" />
            </ToolbarIconButton>

            <MenuPopover<TasksSortField>
              icon={<ArrowUpDown className="h-3.5 w-3.5" />}
              title="Sort"
              options={SORT_OPTIONS}
              selected={sortField}
              trailing={() => (
                <span className="text-xs text-[var(--mute)]">
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
              icon={<Layers className="h-3.5 w-3.5" />}
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
        <div className="font-editorial-serif text-xl italic text-[var(--mute)]">
          Loading…
        </div>
      ) : issues.length === 0 ? (
        <PanelEmptyState
          icon={ListChecks}
          headline="No tasks yet."
          supporting="Create a task to track a piece of work — assign it to an agent and follow it through to done."
          action={{ label: "New task", onClick: onNewTask }}
        />
      ) : viewMode === "board" ? (
        <div className="h-[calc(100vh-18rem)] min-h-[24rem]">
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
                <GroupHeader label={group.label} count={group.items.length} />
              )}
              {renderRows(group.items)}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
