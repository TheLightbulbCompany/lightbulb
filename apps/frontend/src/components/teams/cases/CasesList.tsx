// apps/frontend/src/components/teams/cases/CasesList.tsx

// Ported from upstream Paperclip's Cases (paperclip/ui/src/pages/Cases.tsx) (MIT, (c) 2025 Paperclip AI).
//
// Markup, filters, columns, sort, grouping and the parent/children tree are
// upstream's. Swapped: TanStack Query -> useTeamsApi (SWR, polled — cases emit
// no live events), router links -> in-shell panel URLs, colours -> our tokens.
// Dropped: the j/k/arrow keyboard navigation (a window-level key listener that
// upstream gates on a per-user setting Teams does not have, in a dock where
// several panels are on screen at once) and with it the hover selection.
// Added: the "not enabled" state for upstream's 403 while the instance flag is off.

"use client";

import { type ReactNode, useState } from "react";
import Link from "next/link";
import {
  ArrowUpDown,
  Check,
  ChevronDown,
  ChevronRight,
  Columns3,
  Filter,
  Layers,
  ListTree,
  Search,
  SearchX,
  X,
  type LucideIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { PageSkeleton } from "@/components/teams/shared/components/PageSkeleton";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import {
  CASE_STATUSES,
  TERMINAL_CASE_STATUSES,
  type CaseStatus,
  type CaseSummary,
  type IssueLabel,
} from "@/components/teams/shared/types";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { handleShellLinkClick } from "@/lib/shellNav";
import { notifyApiError } from "@/lib/notify";
import { cn } from "@/lib/utils";
import { CaseCopyableToken } from "./CaseIdentifierKey";
import { CaseStatusPicker } from "./CaseStatusBadge";
import { caseHref } from "./caseNav";
import { isCasesDisabled, useCaseMutations, useCasesList } from "./hooks/useCases";

type GroupBy = "type" | "project" | "status" | "none";
type CaseColumn = "id" | "key" | "title" | "status" | "updated" | "created" | "type" | "project" | "parent";
type CaseSortField = "updated" | "created" | "title" | "status" | "id" | "type" | "project";
type CaseViewState = {
  search: string;
  typeFilters: string[];
  statusFilters: CaseStatus[];
  projectFilters: string[];
  labelFilter: string;
  groupBy: GroupBy;
  sortField: CaseSortField;
  sortDir: "asc" | "desc";
  columns: CaseColumn[];
  treeView: boolean;
};

const STATUS_FILTER_OPTIONS: { value: CaseStatus; label: string }[] = [
  { value: "draft", label: "Draft" },
  { value: "in_progress", label: "In progress" },
  { value: "in_review", label: "In review" },
  { value: "approved", label: "Approved" },
  { value: "done", label: "Done" },
  { value: "cancelled", label: "Cancelled" },
];

const ALL = "__all__";
const DEFAULT_STATUS_FILTERS: CaseStatus[] = CASE_STATUSES.filter((status) => !TERMINAL_CASE_STATUSES.includes(status));
const DEFAULT_CASE_COLUMNS: CaseColumn[] = ["id", "title", "status", "updated"];
const CASE_COLUMN_ORDER: CaseColumn[] = ["id", "key", "title", "type", "status", "updated", "created", "project", "parent"];
const CASE_COLUMN_LABELS: Record<CaseColumn, string> = {
  id: "ID",
  key: "Key",
  title: "Title",
  status: "Status",
  updated: "Updated",
  created: "Created at",
  type: "Type",
  project: "Project",
  parent: "Parent case",
};
const CASE_SORT_LABELS: Record<CaseSortField, string> = {
  updated: "Last updated",
  created: "Created at",
  title: "Title",
  status: "Status",
  id: "ID",
  type: "Type",
  project: "Project",
};
const defaultCaseViewState: CaseViewState = {
  search: "",
  typeFilters: [],
  statusFilters: DEFAULT_STATUS_FILTERS,
  projectFilters: [],
  labelFilter: ALL,
  groupBy: "type",
  sortField: "updated",
  sortDir: "desc",
  columns: DEFAULT_CASE_COLUMNS,
  treeView: false,
};

function normalizeCaseColumns(value: unknown): CaseColumn[] {
  if (!Array.isArray(value)) return DEFAULT_CASE_COLUMNS;
  const valid = CASE_COLUMN_ORDER.filter((column) => value.includes(column));
  return valid.length > 0 ? valid : DEFAULT_CASE_COLUMNS;
}

function normalizeCaseStatuses(value: unknown): CaseStatus[] {
  if (!Array.isArray(value)) return DEFAULT_STATUS_FILTERS;
  const valid = CASE_STATUSES.filter((status) => value.includes(status));
  return valid.length > 0 ? valid : DEFAULT_STATUS_FILTERS;
}

function normalizeStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function normalizeGroupBy(value: unknown): GroupBy {
  return value === "project" || value === "status" || value === "type" || value === "none" ? value : defaultCaseViewState.groupBy;
}

function normalizeSortField(value: unknown): CaseSortField {
  return value === "created"
    || value === "title"
    || value === "status"
    || value === "id"
    || value === "type"
    || value === "project"
    || value === "updated"
    ? value
    : defaultCaseViewState.sortField;
}

function loadCaseViewState(storageKey: string | null): CaseViewState {
  if (typeof window === "undefined" || !storageKey) return { ...defaultCaseViewState };
  try {
    const parsed = JSON.parse(window.localStorage.getItem(storageKey) ?? "null");
    if (!parsed || typeof parsed !== "object") return { ...defaultCaseViewState };
    const record = parsed as Record<string, unknown>;
    const treeView = record.treeView === true;
    const columns = treeView
      ? normalizeCaseColumns([...normalizeCaseColumns(record.columns), "type"])
      : normalizeCaseColumns(record.columns);
    return {
      search: typeof record.search === "string" ? record.search : defaultCaseViewState.search,
      typeFilters: normalizeStringArray(record.typeFilters),
      statusFilters: normalizeCaseStatuses(record.statusFilters),
      projectFilters: normalizeStringArray(record.projectFilters),
      labelFilter: typeof record.labelFilter === "string" ? record.labelFilter : defaultCaseViewState.labelFilter,
      groupBy: treeView ? "none" : normalizeGroupBy(record.groupBy),
      sortField: normalizeSortField(record.sortField),
      sortDir: record.sortDir === "asc" ? "asc" : "desc",
      columns,
      treeView,
    };
  } catch {
    return { ...defaultCaseViewState };
  }
}

function saveCaseViewState(storageKey: string | null, state: CaseViewState) {
  if (typeof window === "undefined" || !storageKey) return;
  try {
    window.localStorage.setItem(storageKey, JSON.stringify(state));
  } catch {
    // Ignore localStorage failures; the controls still work for the session.
  }
}

function caseTrailingGridTemplate(columns: CaseColumn[]): string {
  return columns
    .map((column) => {
      if (column === "title") return "minmax(12rem, 1fr)";
      if (column === "id") return "max-content";
      if (column === "key") return "minmax(8rem, 12rem)";
      if (column === "status") return "minmax(6rem, 7rem)";
      if (column === "type") return "minmax(5rem, 8rem)";
      if (column === "project") return "minmax(5rem, 8rem)";
      if (column === "parent") return "minmax(4rem, 6rem)";
      return "minmax(5rem, 6rem)";
    })
    .join(" ");
}

function sameSet(a: readonly string[], b: readonly string[]) {
  return a.length === b.length && a.every((value) => b.includes(value));
}

function ensureCaseColumn(columns: readonly CaseColumn[], column: CaseColumn): CaseColumn[] {
  return columns.includes(column) ? [...columns] : normalizeCaseColumns([...columns, column]);
}

function caseMatchesViewFilters(caseRow: CaseSummary, viewState: CaseViewState) {
  if (caseRow.matchesListFilters === false) return false;
  if (viewState.typeFilters.length > 0 && !viewState.typeFilters.includes(caseRow.caseType)) return false;
  if (!viewState.statusFilters.includes(caseRow.status)) return false;
  if (viewState.projectFilters.length > 0) {
    const projectKey = caseRow.projectId ?? ALL;
    if (!viewState.projectFilters.includes(projectKey)) return false;
  }
  return true;
}

function treeTitleIndentClass(depth: number): string {
  if (depth <= 0) return "";
  if (depth === 1) return "pl-4";
  if (depth === 2) return "pl-8";
  if (depth === 3) return "pl-12";
  return "pl-16";
}

type CaseTreeRow = {
  row: CaseSummary;
  depth: number;
  childCount: number;
  collapsed: boolean;
};

/** Flatten the visible cases (plus the ancestors that give them context) into
 *  depth-annotated rows, honouring collapsed parents. Cycle-safe. */
function buildCaseTreeRows(
  sortedTreeSource: CaseSummary[],
  filtered: CaseSummary[],
  collapsedTreeCaseIds: ReadonlySet<string>,
): CaseTreeRow[] {
  const rowById = new Map(sortedTreeSource.map((row) => [row.id, row]));
  const visibleIds = new Set(filtered.map((row) => row.id));
  for (const row of filtered) {
    const seen = new Set<string>([row.id]);
    let parentCaseId = row.parentCaseId;
    while (parentCaseId && !seen.has(parentCaseId)) {
      const parent = rowById.get(parentCaseId);
      if (!parent) break;
      visibleIds.add(parent.id);
      seen.add(parent.id);
      parentCaseId = parent.parentCaseId;
    }
  }

  const childrenByParent = new Map<string, CaseSummary[]>();
  for (const row of sortedTreeSource) {
    if (
      !visibleIds.has(row.id)
      || !row.parentCaseId
      || !visibleIds.has(row.parentCaseId)
      || !rowById.has(row.parentCaseId)
    ) {
      continue;
    }
    const children = childrenByParent.get(row.parentCaseId) ?? [];
    children.push(row);
    childrenByParent.set(row.parentCaseId, children);
  }

  const roots = sortedTreeSource.filter((row) =>
    visibleIds.has(row.id)
    && (!row.parentCaseId || !visibleIds.has(row.parentCaseId) || !rowById.has(row.parentCaseId))
  );
  const rows: CaseTreeRow[] = [];
  const visited = new Set<string>();
  const hidden = new Set<string>();

  function markHidden(row: CaseSummary, ancestors: Set<string>) {
    if (hidden.has(row.id) || ancestors.has(row.id)) return;
    hidden.add(row.id);
    const nextAncestors = new Set(ancestors);
    nextAncestors.add(row.id);
    for (const child of childrenByParent.get(row.id) ?? []) {
      markHidden(child, nextAncestors);
    }
  }

  function walk(row: CaseSummary, depth: number, ancestors: Set<string>) {
    if (visited.has(row.id) || hidden.has(row.id)) return;
    visited.add(row.id);
    const children = childrenByParent.get(row.id) ?? [];
    const collapsed = collapsedTreeCaseIds.has(row.id);
    rows.push({ row, depth, childCount: children.length, collapsed });
    if (ancestors.has(row.id)) return;
    const nextAncestors = new Set(ancestors);
    nextAncestors.add(row.id);
    if (collapsed) {
      for (const child of children) {
        markHidden(child, nextAncestors);
      }
      return;
    }
    for (const child of children) {
      walk(child, depth + 1, nextAncestors);
    }
  }

  for (const root of roots) {
    walk(root, 0, new Set());
  }
  for (const row of sortedTreeSource) {
    if (!visibleIds.has(row.id)) continue;
    walk(row, 0, new Set());
  }

  return rows;
}

function CaseTrailingColumns({
  row,
  columns,
  projectName,
  onStatusChange,
  statusPending,
  treeDepth = 0,
  childCount = 0,
  treeView = false,
  treeCollapsed = false,
  onTreeToggle,
}: {
  row: CaseSummary;
  columns: CaseColumn[];
  projectName: string | null;
  onStatusChange: (caseId: string, status: CaseStatus) => void;
  statusPending: boolean;
  treeDepth?: number;
  childCount?: number;
  treeView?: boolean;
  treeCollapsed?: boolean;
  onTreeToggle?: (caseId: string) => void;
}) {
  return (
    <span className="grid min-w-0 flex-1 items-center gap-2" style={{ gridTemplateColumns: caseTrailingGridTemplate(columns) }}>
      {columns.map((column) => {
        if (column === "id") {
          return (
            <CaseCopyableToken
              key={column}
              value={row.identifier}
              label="case ID"
              className="font-mono text-xs text-[var(--fg-mute)]"
              containerClassName="shrink-0"
              stopPropagation
            />
          );
        }
        if (column === "key") {
          return row.key ? (
            <CaseCopyableToken
              key={column}
              value={row.key}
              label="case key"
              className="font-mono text-xs text-[var(--fg-mute)]"
              stopPropagation
            />
          ) : (
            <span key={column} className="min-w-0 truncate text-xs text-[var(--fg-mute)]">None</span>
          );
        }
        if (column === "title") {
          return (
            <span
              key={column}
              className={cn(
                "min-w-0",
                treeView ? "flex items-center gap-1.5" : "truncate text-sm",
                treeView && treeTitleIndentClass(treeDepth),
              )}
            >
              {treeView ? (
                <span className="flex w-4 shrink-0 items-center text-[var(--fg-mute)]">
                  {childCount > 0 ? (
                    <button
                      type="button"
                      className="flex h-4 w-4 items-center justify-center rounded-sm transition-colors hover:bg-[var(--plane-raised)]"
                      aria-label={`${treeCollapsed ? "Expand" : "Collapse"} ${row.title}`}
                      aria-expanded={!treeCollapsed}
                      onClick={(event) => {
                        event.preventDefault();
                        event.stopPropagation();
                        onTreeToggle?.(row.id);
                      }}
                    >
                      <ChevronDown className={cn("h-3.5 w-3.5 transition-transform", treeCollapsed && "-rotate-90")} />
                    </button>
                  ) : treeDepth > 0 ? (
                    <span className="h-px w-3 bg-[var(--edge)]" />
                  ) : null}
                </span>
              ) : null}
              <span className="min-w-0 truncate text-sm">{row.title}</span>
            </span>
          );
        }
        if (column === "status") {
          return (
            <span key={column} className="min-w-0">
              <CaseStatusPicker
                status={row.status}
                disabled={statusPending}
                onChange={(status) => onStatusChange(row.id, status)}
              />
            </span>
          );
        }
        if (column === "type") {
          return <span key={column} className="min-w-0 truncate text-xs text-[var(--fg-mute)]">{row.caseType}</span>;
        }
        if (column === "project") {
          return <span key={column} className="min-w-0 truncate text-xs text-[var(--fg-mute)]">{projectName ?? "No project"}</span>;
        }
        if (column === "parent") {
          return (
            <span key={column} className="min-w-0 truncate font-mono text-xs text-[var(--fg-mute)]">
              {row.parentCaseId ? "Parent" : "None"}
            </span>
          );
        }
        return (
          <span key={column} className="text-right text-xs text-[var(--fg-mute)] tabular-nums">
            {timeAgo(column === "created" ? row.createdAt : row.updatedAt)}
          </span>
        );
      })}
    </span>
  );
}

function CaseListRow({
  row,
  projectName,
  visibleColumnSet,
  trailingColumns,
  onStatusChange,
  statusPending,
  treeDepth,
  childCount,
  treeView,
  treeCollapsed,
  onTreeToggle,
}: {
  row: CaseSummary;
  projectName: string | null;
  visibleColumnSet: ReadonlySet<CaseColumn>;
  trailingColumns: CaseColumn[];
  onStatusChange: (caseId: string, status: CaseStatus) => void;
  statusPending: boolean;
  treeDepth?: number;
  childCount?: number;
  treeView?: boolean;
  treeCollapsed?: boolean;
  onTreeToggle?: (caseId: string) => void;
}) {
  const href = caseHref(row.identifier);
  return (
    <Link
      data-case-item
      href={href}
      prefetch={false}
      onClick={(e) => handleShellLinkClick(e, href)}
      className="group flex items-start gap-2 border-b border-[var(--edge)] py-2.5 pl-2 pr-3 text-sm text-inherit no-underline transition-colors last:border-b-0 hover:bg-[var(--plane-raised)] sm:items-center sm:py-2 sm:pl-1"
    >
      <span className="flex min-w-0 flex-1 flex-col gap-1 sm:contents">
        <span className="line-clamp-2 text-sm sm:hidden">
          {visibleColumnSet.has("title") ? row.title : row.identifier}
        </span>
        <span className="flex items-center gap-2 sm:hidden">
          {visibleColumnSet.has("status") ? (
            <CaseStatusPicker
              status={row.status}
              disabled={statusPending}
              onChange={(status) => onStatusChange(row.id, status)}
            />
          ) : null}
          {visibleColumnSet.has("id") ? (
            <CaseCopyableToken
              value={row.identifier}
              label="case ID"
              className="font-mono text-xs text-[var(--fg-mute)]"
              containerClassName="shrink-0"
              stopPropagation
            />
          ) : null}
          {visibleColumnSet.has("key") && row.key ? (
            <CaseCopyableToken
              value={row.key}
              label="case key"
              className="shrink-0 font-mono text-xs text-[var(--fg-mute)]"
              stopPropagation
            />
          ) : null}
          <span className="text-xs text-[var(--fg-mute)] sm:hidden">{timeAgo(row.updatedAt)}</span>
        </span>
      </span>
      {trailingColumns.length > 0 ? (
        <span className="hidden min-w-0 flex-1 items-center gap-3 sm:order-3 sm:flex">
          <CaseTrailingColumns
            row={row}
            columns={trailingColumns}
            projectName={projectName}
            statusPending={statusPending}
            onStatusChange={onStatusChange}
            treeDepth={treeDepth}
            childCount={childCount}
            treeView={treeView}
            treeCollapsed={treeCollapsed}
            onTreeToggle={onTreeToggle}
          />
        </span>
      ) : null}
    </Link>
  );
}

function CaseColumnHeader({ trailingColumns }: { trailingColumns: CaseColumn[] }) {
  return (
    <div className="hidden border-b border-[var(--edge)] px-2 py-1 text-2xs font-medium uppercase tracking-wide text-[var(--fg-mute)] sm:flex sm:items-center">
      {trailingColumns.length > 0 ? (
        <span className="grid min-w-0 flex-1 items-center gap-2" style={{ gridTemplateColumns: caseTrailingGridTemplate(trailingColumns) }}>
          {trailingColumns.map((column) => (
            <span key={column} className={cn("truncate", (column === "updated" || column === "created") && "text-right")}>
              {CASE_COLUMN_LABELS[column]}
            </span>
          ))}
        </span>
      ) : null}
    </div>
  );
}

/** Upstream's IssueGroupHeader (collapsible form), inlined: Teams has no shared twin. */
function CaseGroup({
  label,
  count,
  collapsed,
  onToggle,
  children,
}: {
  label: string;
  count: number;
  collapsed: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <div className="mt-6 first:mt-0">
      <div className="flex items-center py-1.5 pl-1 pr-3">
        <button
          type="button"
          className="flex min-w-0 items-center gap-2 text-left"
          aria-expanded={!collapsed}
          onClick={onToggle}
        >
          <span className="inline-flex w-4 shrink-0 items-center justify-center">
            <ChevronRight
              className={cn("h-3.5 w-3.5 shrink-0 text-[var(--fg-mute)] transition-transform", !collapsed && "rotate-90")}
            />
          </span>
          <span className="truncate text-sm font-semibold uppercase tracking-wide">{label}</span>
        </button>
        <div className="ml-auto">
          <span className="text-xs text-[var(--fg-mute)] tabular-nums">
            {count} {count === 1 ? "case" : "cases"}
          </span>
        </div>
      </div>
      {!collapsed && <div>{children}</div>}
    </div>
  );
}

function CaseToolbarButton({
  icon: Icon,
  title,
  active,
  children,
}: {
  icon: LucideIcon;
  title: string;
  active?: boolean;
  children: ReactNode;
}) {
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="outline"
          size="icon"
          className={cn("h-8 w-8 shrink-0", active && "bg-[var(--plane-raised)]")}
          title={title}
          aria-label={title}
        >
          <Icon className="h-3.5 w-3.5" />
        </Button>
      </PopoverTrigger>
      {children}
    </Popover>
  );
}

function FilterField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid gap-1">
      <div className="text-xs font-medium text-[var(--fg-mute)]">{label}</div>
      {children}
    </div>
  );
}

function FilterCheckboxRow({
  label,
  checked,
  onCheckedChange,
}: {
  label: string;
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
}) {
  return (
    <label className="flex cursor-pointer items-center gap-2 rounded px-1 py-1 text-sm hover:bg-[var(--plane-raised)]">
      <Checkbox
        checked={checked}
        onCheckedChange={(value) => onCheckedChange(value === true)}
        onClick={(event) => event.stopPropagation()}
      />
      <span className="min-w-0 flex-1 truncate">{label}</span>
    </label>
  );
}

function CaseColumnPicker({
  visibleColumns,
  onToggle,
  onReset,
}: {
  visibleColumns: ReadonlySet<CaseColumn>;
  onToggle: (column: CaseColumn, enabled: boolean) => void;
  onReset: () => void;
}) {
  return (
    <CaseToolbarButton icon={Columns3} title="Columns" active={!sameSet([...visibleColumns], DEFAULT_CASE_COLUMNS)}>
      <PopoverContent align="end" className="w-[300px] p-1.5">
        <div className="px-2 pb-1 pt-1.5">
          <div className="text-2xs font-semibold uppercase tracking-wide text-[var(--fg-mute)]">
            Desktop case rows
          </div>
          <div className="text-sm font-medium text-[var(--fg)]">Choose visible columns</div>
        </div>
        <div className="space-y-0.5">
          {CASE_COLUMN_ORDER.map((column) => (
            <button
              key={column}
              type="button"
              className="flex w-full items-center justify-between rounded px-2 py-1.5 text-sm hover:bg-[var(--plane-raised)]"
              onClick={() => onToggle(column, !visibleColumns.has(column))}
            >
              <span>{CASE_COLUMN_LABELS[column]}</span>
              {visibleColumns.has(column) ? <Check className="h-3.5 w-3.5 text-[var(--fg-mute)]" /> : null}
            </button>
          ))}
        </div>
        <div className="mt-1 border-t border-[var(--edge)] pt-1">
          <button
            type="button"
            className="flex w-full items-center justify-between rounded px-2 py-1.5 text-sm text-[var(--fg-mute)] hover:bg-[var(--plane-raised)]"
            onClick={onReset}
          >
            Reset defaults
          </button>
        </div>
      </PopoverContent>
    </CaseToolbarButton>
  );
}

function CaseSortPicker({
  sortField,
  sortDir,
  onChange,
}: {
  sortField: CaseSortField;
  sortDir: "asc" | "desc";
  onChange: (patch: Pick<CaseViewState, "sortField" | "sortDir">) => void;
}) {
  return (
    <CaseToolbarButton icon={ArrowUpDown} title="Sort" active={sortField !== "updated" || sortDir !== "desc"}>
      <PopoverContent align="end" className="w-48 p-2">
        {(Object.keys(CASE_SORT_LABELS) as CaseSortField[]).map((field) => (
          <button
            key={field}
            type="button"
            className={cn(
              "flex w-full items-center justify-between rounded-sm px-2 py-1.5 text-sm",
              sortField === field ? "bg-[var(--plane-raised)] text-[var(--fg)]" : "text-[var(--fg-mute)] hover:bg-[var(--plane-raised)]",
            )}
            onClick={() => {
              if (sortField === field) {
                onChange({ sortField, sortDir: sortDir === "asc" ? "desc" : "asc" });
              } else {
                onChange({ sortField: field, sortDir: field === "updated" || field === "created" ? "desc" : "asc" });
              }
            }}
          >
            <span>{CASE_SORT_LABELS[field]}</span>
            {sortField === field ? (
              <span className="text-xs text-[var(--fg-mute)]">{sortDir === "asc" ? "↑" : "↓"}</span>
            ) : null}
          </button>
        ))}
      </PopoverContent>
    </CaseToolbarButton>
  );
}

/** Upstream's FilterBar, inlined: Teams has no shared twin. */
function FilterBar({
  filters,
  onRemove,
  onClear,
}: {
  filters: { key: string; label: string; value: string }[];
  onRemove: (key: string) => void;
  onClear: () => void;
}) {
  if (filters.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-2">
      {filters.map((f) => (
        <Badge key={f.key} variant="secondary" className="gap-1 pr-1">
          <span className="text-[var(--fg-mute)]">{f.label}:</span>
          <span>{f.value}</span>
          <button
            type="button"
            className="ml-1 rounded-full p-0.5 hover:bg-[var(--plane-selected)]"
            aria-label={`Remove ${f.label} filter`}
            onClick={() => onRemove(f.key)}
          >
            <X className="h-3 w-3" />
          </button>
        </Badge>
      ))}
      <Button variant="ghost" size="sm" className="h-6 text-xs" onClick={onClear}>
        Clear all
      </Button>
    </div>
  );
}

/** Full-page onboarding hero shown when the company has zero cases. */
function CasesEmptyHero() {
  return (
    <div className="mx-auto flex max-w-xl flex-col items-center gap-4 py-16 text-center">
      <Layers className="h-10 w-10 text-[var(--fg-mute)]" />
      <h2 className="text-lg font-semibold">No cases yet</h2>
      <p className="text-sm text-[var(--fg-mute)]">
        Cases are durable work products — blog posts, tweet storms, docs pages — that tasks create and
        iterate on. In v1 they&apos;re created by agents, not from the UI.
      </p>
      <div className="w-full space-y-2 rounded-lg border border-[var(--edge)] bg-[var(--plane-sidebar)] p-4 text-left">
        <p className="text-sm font-medium">To start creating cases, add this to a skill:</p>
        <pre className="overflow-x-auto rounded bg-[var(--plane-pane)] p-3 font-mono text-xs text-[var(--fg-mute)]">
{`"Create a case of type blog_post with fields
{slug, target_audience, publish_url} and key <release>/<slug>."`}
        </pre>
        <p className="text-xs text-[var(--fg-mute)]">
          See the paperclip skill → <code className="font-mono">references/cases.md</code> for the API.
        </p>
      </div>
    </div>
  );
}

/** Shown while the instance flag is off (upstream answers 403 "Cases are disabled"). */
function CasesNotEnabled() {
  return (
    <div className="mx-auto flex max-w-xl flex-col items-center gap-4 py-16 text-center">
      <Layers className="h-10 w-10 text-[var(--fg-mute)]" />
      <h2 className="text-lg font-semibold">Cases are not enabled for this workspace yet</h2>
    </div>
  );
}

interface ProjectRow {
  id: string;
  name: string;
  archivedAt?: string | null;
}

export function CasesList({ storageKey = null }: { storageKey?: string | null }) {
  const { read } = useTeamsApi();
  const { patchCase } = useCaseMutations();

  const [viewState, setViewState] = useState<CaseViewState>(() => loadCaseViewState(storageKey));
  const [collapsedTreeCaseIds, setCollapsedTreeCaseIds] = useState<Set<string>>(() => new Set());
  const [collapsedGroupKeys, setCollapsedGroupKeys] = useState<Set<string>>(() => new Set());
  const [pendingCaseId, setPendingCaseId] = useState<string | null>(null);

  function updateView(patch: Partial<CaseViewState>) {
    const next = { ...viewState, ...patch };
    if (viewState.treeView && patch.groupBy && patch.groupBy !== "none") {
      next.treeView = false;
    }
    if (patch.treeView === true) {
      next.groupBy = "none";
      next.columns = ensureCaseColumn(next.columns, "type");
    } else if (next.treeView) {
      next.groupBy = "none";
    }
    saveCaseViewState(storageKey, next);
    setViewState(next);
  }

  const usesDefaultStatusFilter = sameSet(viewState.statusFilters, DEFAULT_STATUS_FILTERS);
  const projectIds = viewState.projectFilters.filter((projectId) => projectId !== ALL);

  const casesQuery = useCasesList({
    types: viewState.typeFilters.length > 0 ? viewState.typeFilters : undefined,
    status: usesDefaultStatusFilter ? "active" : undefined,
    statuses:
      usesDefaultStatusFilter || viewState.statusFilters.length === CASE_STATUSES.length
        ? undefined
        : viewState.statusFilters,
    projectIds: projectIds.length > 0 ? projectIds : undefined,
    includeNoProject: viewState.projectFilters.includes(ALL) || undefined,
    labelId: viewState.labelFilter === ALL ? undefined : viewState.labelFilter,
    q: viewState.search.trim() || undefined,
    includeAncestors: viewState.treeView ? true : undefined,
    limit: 200,
  });
  const projectsQuery = read<{ projects: ProjectRow[] }>("/projects");
  const labelsQuery = read<{ labels: IssueLabel[] }>("/issues/labels");

  const allCases = casesQuery.data?.cases ?? [];
  const projects = projectsQuery.data?.projects ?? [];
  const labels = labelsQuery.data?.labels ?? [];
  const projectName = new Map(projects.map((p) => [p.id, p.name]));

  const distinctTypes = [...new Set([...allCases.map((c) => c.caseType), ...viewState.typeFilters])].sort();
  const filtered = allCases.filter((caseRow) => caseMatchesViewFilters(caseRow, viewState));

  const caseSortCompare = (a: CaseSummary, b: CaseSummary) => {
    let result = 0;
    if (viewState.sortField === "updated") result = Date.parse(a.updatedAt) - Date.parse(b.updatedAt);
    else if (viewState.sortField === "created") result = Date.parse(a.createdAt) - Date.parse(b.createdAt);
    else if (viewState.sortField === "title") result = a.title.localeCompare(b.title);
    else if (viewState.sortField === "status") result = a.status.localeCompare(b.status);
    else if (viewState.sortField === "id") result = a.identifier.localeCompare(b.identifier);
    else if (viewState.sortField === "type") result = a.caseType.localeCompare(b.caseType);
    else {
      const aProject = a.projectId ? projectName.get(a.projectId) ?? "" : "";
      const bProject = b.projectId ? projectName.get(b.projectId) ?? "" : "";
      result = aProject.localeCompare(bProject);
    }
    return viewState.sortDir === "desc" ? -result : result;
  };
  const sorted = [...filtered].sort(caseSortCompare);

  const visibleColumnSet = new Set(viewState.columns);
  const trailingColumns = CASE_COLUMN_ORDER.filter((column) => visibleColumnSet.has(column));

  let groupedRows: { key: string; label: string | null; rows: CaseSummary[] }[];
  if (viewState.groupBy === "none") {
    groupedRows = [{ key: "__all__", label: null, rows: sorted }];
  } else {
    const map = new Map<string, CaseSummary[]>();
    for (const c of sorted) {
      let key: string;
      if (viewState.groupBy === "type") key = c.caseType;
      else if (viewState.groupBy === "status") key = c.status;
      else key = c.projectId ? projectName.get(c.projectId) ?? "Unknown project" : "No project";
      const bucket = map.get(key);
      if (bucket) bucket.push(c);
      else map.set(key, [c]);
    }
    groupedRows = [...map.entries()]
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([label, rows]) => ({ key: label, label, rows }));
  }

  const treeRows = viewState.treeView
    ? buildCaseTreeRows([...allCases].sort(caseSortCompare), filtered, collapsedTreeCaseIds)
    : [];

  const activeFilters: { key: string; label: string; value: string }[] = [];
  if (viewState.search.trim()) activeFilters.push({ key: "search", label: "Search", value: viewState.search.trim() });
  if (viewState.typeFilters.length > 0) {
    activeFilters.push({ key: "type", label: "Type", value: viewState.typeFilters.join(", ") });
  }
  if (!usesDefaultStatusFilter) {
    activeFilters.push({
      key: "status",
      label: "Status",
      value: viewState.statusFilters.length === CASE_STATUSES.length
        ? "All"
        : viewState.statusFilters
          .map((status) => STATUS_FILTER_OPTIONS.find((option) => option.value === status)?.label ?? status)
          .join(", "),
    });
  }
  if (viewState.projectFilters.length > 0) {
    activeFilters.push({
      key: "project",
      label: "Project",
      value: viewState.projectFilters
        .map((projectId) => projectId === ALL ? "No project" : projectName.get(projectId) ?? "Project")
        .join(", "),
    });
  }
  if (viewState.labelFilter !== ALL) {
    const name = labels.find((l) => l.id === viewState.labelFilter)?.name ?? "Label";
    activeFilters.push({ key: "label", label: "Label", value: name });
  }

  function removeFilter(key: string) {
    if (key === "search") updateView({ search: "" });
    else if (key === "type") updateView({ typeFilters: [] });
    else if (key === "status") updateView({ statusFilters: DEFAULT_STATUS_FILTERS });
    else if (key === "project") updateView({ projectFilters: [] });
    else if (key === "label") updateView({ labelFilter: ALL });
  }
  function clearFilters() {
    updateView({
      search: "",
      typeFilters: [],
      statusFilters: DEFAULT_STATUS_FILTERS,
      projectFilters: [],
      labelFilter: ALL,
    });
  }
  function toggleColumn(column: CaseColumn, enabled: boolean) {
    const next = enabled
      ? [...viewState.columns, column]
      : viewState.columns.filter((value) => value !== column);
    updateView({ columns: normalizeCaseColumns(next) });
  }
  function resetColumns() {
    updateView({ columns: viewState.treeView ? ensureCaseColumn(DEFAULT_CASE_COLUMNS, "type") : DEFAULT_CASE_COLUMNS });
  }
  function toggleIn(current: Set<string>, id: string): Set<string> {
    const next = new Set(current);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    return next;
  }
  function toggleStringFilter(key: "typeFilters" | "projectFilters", value: string, enabled: boolean) {
    const current = viewState[key];
    updateView({
      [key]: enabled
        ? [...current, value]
        : current.filter((item) => item !== value),
    });
  }
  function toggleStatusFilter(status: CaseStatus, enabled: boolean) {
    const next = enabled
      ? [...viewState.statusFilters, status]
      : viewState.statusFilters.filter((item) => item !== status);
    updateView({ statusFilters: normalizeCaseStatuses(next) });
  }
  async function changeStatus(caseId: string, status: CaseStatus) {
    setPendingCaseId(caseId);
    try {
      await patchCase(caseId, { status });
    } catch (e) {
      notifyApiError(e, "Couldn't update the case.");
    } finally {
      setPendingCaseId(null);
    }
  }

  const header = (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex items-center gap-2">
        <h1 className="text-xl font-bold">Cases</h1>
        <Badge variant="secondary">Experimental</Badge>
      </div>
    </div>
  );

  if (casesQuery.error && !casesQuery.data) {
    return (
      <div className="space-y-4">
        {header}
        {isCasesDisabled(casesQuery.error) ? (
          <CasesNotEnabled />
        ) : (
          <p role="alert" className="py-16 text-center text-sm text-[var(--alert)]">
            Couldn&apos;t load cases.
          </p>
        )}
      </div>
    );
  }
  if (!casesQuery.data) return <PageSkeleton />;

  const hasActiveFilters = activeFilters.length > 0;
  const noCasesAtAll = allCases.length === 0 && !hasActiveFilters;

  const renderRow = (row: CaseSummary, tree?: CaseTreeRow) => (
    <CaseListRow
      key={row.id}
      row={row}
      projectName={row.projectId ? projectName.get(row.projectId) ?? null : null}
      visibleColumnSet={visibleColumnSet}
      trailingColumns={trailingColumns}
      statusPending={pendingCaseId === row.id}
      onStatusChange={(caseId, status) => void changeStatus(caseId, status)}
      {...(tree
        ? {
            treeView: true,
            treeDepth: tree.depth,
            childCount: tree.childCount,
            treeCollapsed: tree.collapsed,
            onTreeToggle: (caseId: string) => setCollapsedTreeCaseIds((current) => toggleIn(current, caseId)),
          }
        : {})}
    />
  );

  return (
    <div className="space-y-4">
      {header}

      {noCasesAtAll ? (
        <CasesEmptyHero />
      ) : (
        <>
          <div className="flex items-center justify-between gap-2 sm:gap-3">
            <div className="relative w-48 sm:w-64 md:w-80">
              <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--fg-mute)]" />
              <Input
                value={viewState.search}
                onChange={(e) => updateView({ search: e.target.value })}
                placeholder="Search cases..."
                className="pl-7 text-xs sm:text-sm"
                aria-label="Search cases"
                data-page-search
              />
            </div>

            <div className="flex shrink-0 items-center gap-0.5 sm:gap-1">
              <Button
                type="button"
                variant="outline"
                size="icon"
                className={cn("h-8 w-8 shrink-0", viewState.treeView && "bg-[var(--plane-raised)]")}
                title={viewState.treeView ? "Show flat case list" : "Show parent/children tree"}
                aria-label={viewState.treeView ? "Show flat case list" : "Show parent/children tree"}
                aria-pressed={viewState.treeView}
                onClick={() => updateView({ treeView: !viewState.treeView })}
              >
                <ListTree className="h-3.5 w-3.5" />
              </Button>

              <CaseColumnPicker
                visibleColumns={visibleColumnSet}
                onToggle={toggleColumn}
                onReset={resetColumns}
              />

              <CaseToolbarButton icon={Filter} title="Filters" active={hasActiveFilters}>
                <PopoverContent align="end" className="w-72 p-3">
                  <div className="grid gap-3">
                    <FilterField label="Type">
                      <div className="max-h-40 overflow-y-auto">
                        {distinctTypes.length === 0 ? (
                          <p className="px-1 py-1 text-xs text-[var(--fg-mute)]">No types yet</p>
                        ) : distinctTypes.map((type) => (
                          <FilterCheckboxRow
                            key={type}
                            label={type}
                            checked={viewState.typeFilters.includes(type)}
                            onCheckedChange={(checked) => toggleStringFilter("typeFilters", type, checked)}
                          />
                        ))}
                      </div>
                    </FilterField>
                    <FilterField label="Status">
                      <div>
                        {STATUS_FILTER_OPTIONS.map((option) => (
                          <FilterCheckboxRow
                            key={option.value}
                            label={option.label}
                            checked={viewState.statusFilters.includes(option.value)}
                            onCheckedChange={(checked) => toggleStatusFilter(option.value, checked)}
                          />
                        ))}
                      </div>
                    </FilterField>
                    <FilterField label="Project">
                      <div className="max-h-40 overflow-y-auto">
                        <FilterCheckboxRow
                          label="No project"
                          checked={viewState.projectFilters.includes(ALL)}
                          onCheckedChange={(checked) => toggleStringFilter("projectFilters", ALL, checked)}
                        />
                        {projects.filter((project) => !project.archivedAt).map((project) => (
                          <FilterCheckboxRow
                            key={project.id}
                            label={project.name}
                            checked={viewState.projectFilters.includes(project.id)}
                            onCheckedChange={(checked) => toggleStringFilter("projectFilters", project.id, checked)}
                          />
                        ))}
                      </div>
                    </FilterField>
                    <FilterField label="Label">
                      <div className="max-h-40 overflow-y-auto">
                        <FilterCheckboxRow
                          label="All labels"
                          checked={viewState.labelFilter === ALL}
                          onCheckedChange={(checked) => {
                            if (checked) updateView({ labelFilter: ALL });
                          }}
                        />
                        {labels.map((label) => (
                          <FilterCheckboxRow
                            key={label.id}
                            label={label.name}
                            checked={viewState.labelFilter === label.id}
                            onCheckedChange={(checked) => updateView({ labelFilter: checked ? label.id : ALL })}
                          />
                        ))}
                      </div>
                    </FilterField>
                    <Button type="button" variant="ghost" size="sm" onClick={clearFilters} disabled={!hasActiveFilters}>
                      Clear filters
                    </Button>
                  </div>
                </PopoverContent>
              </CaseToolbarButton>

              <CaseSortPicker
                sortField={viewState.sortField}
                sortDir={viewState.sortDir}
                onChange={updateView}
              />

              <CaseToolbarButton icon={Layers} title="Group" active={viewState.groupBy !== "type"}>
                <PopoverContent align="end" className="w-44 p-2">
                  {([
                    ["type", "Type"],
                    ["project", "Project"],
                    ["status", "Status"],
                    ["none", "None"],
                  ] as const).map(([value, label]) => (
                    <button
                      key={value}
                      type="button"
                      className={cn(
                        "flex w-full items-center justify-between rounded-sm px-2 py-1.5 text-sm",
                        viewState.groupBy === value ? "bg-[var(--plane-raised)] text-[var(--fg)]" : "text-[var(--fg-mute)] hover:bg-[var(--plane-raised)]",
                      )}
                      onClick={() => updateView({ groupBy: value })}
                    >
                      <span>{label}</span>
                      {viewState.groupBy === value && <Check className="h-3.5 w-3.5" />}
                    </button>
                  ))}
                </PopoverContent>
              </CaseToolbarButton>
            </div>
          </div>

          <FilterBar filters={activeFilters} onRemove={removeFilter} onClear={clearFilters} />

          {filtered.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-16 text-center">
              <div className="mb-4 bg-[var(--plane-sidebar)] p-4">
                <SearchX className="h-10 w-10 text-[var(--fg-mute)]" />
              </div>
              <p className="mb-4 text-sm font-medium text-[var(--fg)]">No cases match these filters.</p>
              <Button onClick={clearFilters}>Clear filters</Button>
            </div>
          ) : (
            <div>
              <CaseColumnHeader trailingColumns={trailingColumns} />
              {viewState.treeView ? (
                treeRows.map((tree) => renderRow(tree.row, tree))
              ) : (
                groupedRows.map((group) => group.label ? (
                  <CaseGroup
                    key={group.key}
                    label={group.label}
                    count={group.rows.length}
                    collapsed={collapsedGroupKeys.has(group.key)}
                    onToggle={() => setCollapsedGroupKeys((current) => toggleIn(current, group.key))}
                  >
                    {group.rows.map((row) => renderRow(row))}
                  </CaseGroup>
                ) : (
                  <div key={group.key}>{group.rows.map((row) => renderRow(row))}</div>
                ))
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
