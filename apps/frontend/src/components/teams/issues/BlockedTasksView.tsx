// apps/frontend/src/components/teams/issues/BlockedTasksView.tsx

// Ported from upstream Paperclip's BlockedInboxView
// (paperclip/ui/src/components/BlockedInboxView.tsx @ v2026.1012.0-isol8.4)
// (MIT, (c) 2025 Paperclip AI). The list query, search -> filter -> sort/group
// pipeline, owner resolution, row layout, loading / error / empty / no-match
// branches and their copy are verbatim, except where listed.
//
// Adaptations vs upstream (only what had to change):
//   - lives in Work -> Tasks as the "Blocked" view, not an Inbox tab
//   - @paperclipai/shared types        -> components/teams/shared/types
//   - react-query issuesApi.list       -> one useTeamsApi.read of the BFF's
//                                         /issues?attention=blocked&... (same params,
//                                         same limit); companyId -> dropped (the BFF
//                                         pins the workspace); optional projectId ->
//                                         the BFF's `project` param, so a project
//                                         page's Blocked view stays in that project
//   - error copy "Other Inbox tabs still work." -> "Other views still work."
//   - lib/issue-filters applyIssueFilters (+ workspace context)
//                                      -> shared/lib/issueFilters applyIssueFilters
//   - agentNameById / userLabelById maps -> derived here from the agents / members
//                                         rosters TasksList already holds
//   - IssueRow "task" presentation     -> not ported; our IssueRow port is the legacy
//                                         layout, so the column toggles and
//                                         presentation props are dropped
//   - lib/inbox-live-descendants resolveInboxIssueBlockerAttention
//                                      -> not ported; IssueRow's own StatusIcon reads
//                                         issue.blockerAttention
//   - IssueGroupHeader                 -> an inline collapsible header button
//   - AgentIdentity / Identity         -> shared Identity for both owner kinds
//   - Card / amber palette / text-sm / text-x-small / --sz-* widths
//                                      -> PanelEmptyState, editorial-card, warn
//                                         token, text-ui / text-caption, w-* utilities

"use client";

import { useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { Button } from "@/components/ui/button";
import { PanelEmptyState } from "@/components/teams/shell";
import { IssueRow } from "@/components/teams/inbox/IssueRow";
import { Identity } from "@/components/teams/shared/components/Identity";
import {
  applyIssueFilters,
  type IssueFilterState,
} from "@/components/teams/shared/lib/issueFilters";
import type { CompanyMember, Issue } from "@/components/teams/shared/types";
import { BlockedReasonChip } from "./BlockedReasonChip";
import {
  blockedRowMatchesSearch,
  buildBlockedInboxRows,
  formatStoppedAge,
  groupBlockedInboxRows,
  sortBlockedInboxRows,
  type BlockedInboxGroupBy,
  type BlockedInboxIssueRow,
  type BlockedInboxSort,
} from "./lib/blockedInbox";

interface BlockedTasksViewProps {
  searchQuery: string;
  agents?: ReadonlyArray<{ id: string; name: string }>;
  members?: ReadonlyArray<CompanyMember>;
  groupBy: BlockedInboxGroupBy;
  sortBy: BlockedInboxSort;
  issueFilters: IssueFilterState;
  currentUserId: string | null;
  liveIssueIds?: ReadonlySet<string>;
  /** Narrows the list to one project (the project page's Tasks tab). */
  projectId?: string;
  agentFilter?: string | null;
}

const BLOCKED_LIST_LIMIT = 200;

export function BlockedTasksView({
  searchQuery,
  agents,
  members,
  groupBy,
  sortBy,
  issueFilters,
  currentUserId,
  liveIssueIds,
  projectId,
  agentFilter = null,
}: BlockedTasksViewProps) {
  const [collapsedVariants, setCollapsedVariants] = useState<Set<string>>(() => new Set());

  const { read } = useTeamsApi();
  const path =
    "/issues?attention=blocked&includeBlockedInboxAttention=true&includeBlockedBy=true" +
    `&includeLiveDescendantSummary=true&limit=${BLOCKED_LIST_LIMIT}` +
    (projectId ? `&project=${encodeURIComponent(projectId)}` : "");
  const { data, isLoading, isValidating, error, mutate } = read<{ issues: Issue[] }>(path);
  const issues = data?.issues;

  const agentNameById = useMemo(
    () => new Map((agents ?? []).map((a) => [a.id, a.name])),
    [agents],
  );
  const userLabelById = useMemo(() => {
    const map = new Map<string, string>();
    for (const m of members ?? []) {
      const label = m.name ?? m.email;
      if (m.paperclipUserId && label) map.set(m.paperclipUserId, label);
    }
    return map;
  }, [members]);

  const allRows = useMemo(() => buildBlockedInboxRows(issues ?? []), [issues]);
  const filteredRows = useMemo(
    () =>
      allRows.filter(
        (row) =>
          (!agentFilter || row.issue.assigneeAgentId === agentFilter) &&
          blockedRowMatchesSearch(row, searchQuery),
      ),
    [agentFilter, allRows, searchQuery],
  );
  const issueFilteredRows = useMemo(() => {
    const visibleIssueIds = new Set(
      applyIssueFilters(
        filteredRows.map((row) => row.issue),
        issueFilters,
        { currentUserId, liveIssueIds },
      ).map((issue) => issue.id),
    );
    return filteredRows.filter((row) => visibleIssueIds.has(row.issue.id));
  }, [currentUserId, filteredRows, issueFilters, liveIssueIds]);
  const sortedRows = useMemo(() => sortBlockedInboxRows(issueFilteredRows, sortBy), [issueFilteredRows, sortBy]);
  const groups = useMemo(
    () => groupBlockedInboxRows(issueFilteredRows, sortBy),
    [issueFilteredRows, sortBy],
  );

  const toggleVariant = (variant: string) => {
    setCollapsedVariants((prev) => {
      const next = new Set(prev);
      if (next.has(variant)) next.delete(variant);
      else next.add(variant);
      return next;
    });
  };

  const renderRow = (row: BlockedInboxIssueRow) => (
    <BlockedTaskRow
      key={row.issue.id}
      row={row}
      agentNameById={agentNameById}
      userLabelById={userLabelById}
    />
  );

  if (isLoading) {
    return (
      <div data-testid="blocked-inbox-loading" className="space-y-3" aria-busy="true">
        {Array.from({ length: 3 }).map((_, groupIdx) => (
          <div key={groupIdx} className="space-y-1">
            <div className="h-4 w-40 animate-pulse rounded bg-[var(--cream-deeper)]" />
            {Array.from({ length: 2 }).map((__, rowIdx) => (
              <div
                key={rowIdx}
                className="flex items-center gap-3 border-b border-[var(--edge)] px-3 py-2.5 sm:px-4"
              >
                <div className="h-3.5 w-3.5 animate-pulse rounded-full bg-[var(--cream-deeper)]" />
                <div className="h-4 w-16 animate-pulse rounded bg-[var(--cream-deep)]" />
                <div className="h-4 w-32 animate-pulse rounded-md bg-[var(--cream-deep)]" />
                <div className="h-4 flex-1 animate-pulse rounded bg-[var(--cream-deep)]" />
                <div className="hidden h-3 w-24 animate-pulse rounded bg-[var(--cream-deep)] sm:block" />
              </div>
            ))}
          </div>
        ))}
      </div>
    );
  }

  if (error) {
    const message = error instanceof Error ? error.message : "Couldn't load the Blocked tab.";
    return (
      <div
        data-testid="blocked-inbox-error"
        role="alert"
        className="flex flex-col gap-2 rounded-md border border-warn/40 bg-warn/10 p-4 text-warn"
      >
        <div className="flex items-start gap-2">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          <div className="flex-1 space-y-1">
            <p className="text-ui font-medium">Couldn&apos;t load the Blocked tab.</p>
            <p className="text-caption opacity-80">Other views still work. {message}</p>
          </div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-7 shrink-0 border-warn/60 text-warn"
            onClick={() => void mutate()}
            disabled={isValidating}
          >
            {isValidating ? "Trying…" : "Try again"}
          </Button>
        </div>
      </div>
    );
  }

  if (allRows.length === 0) {
    return (
      <PanelEmptyState
        icon={CheckCircle2}
        headline="No work is stopped."
        supporting="Tasks that need a decision, recovery, or external action will appear here."
      />
    );
  }

  if (groups.length === 0) {
    return (
      <div
        data-testid="blocked-inbox-no-search-results"
        className="editorial-card px-4 py-6 text-center text-ui text-[var(--midnight-muted)]"
      >
        No stopped items match your search.
      </div>
    );
  }

  return (
    <div data-testid="blocked-inbox" className="editorial-card overflow-hidden">
      {groupBy === "none"
        ? sortedRows.map(renderRow)
        : groups.map((group) => {
            const isCollapsed = collapsedVariants.has(group.variant);
            return (
              <div key={group.variant} data-testid={`blocked-inbox-group-${group.variant}`}>
                <button
                  type="button"
                  onClick={() => toggleVariant(group.variant)}
                  aria-expanded={!isCollapsed}
                  className="flex w-full items-center gap-1 px-3 pb-1.5 pt-3 text-left sm:px-4"
                >
                  <ChevronRight
                    size={12}
                    aria-hidden="true"
                    className={cn(
                      "shrink-0 text-[var(--midnight-muted)] transition-transform",
                      !isCollapsed && "rotate-90",
                    )}
                  />
                  <span className="label-caps text-[var(--midnight-muted)]">
                    {`${group.label} · ${group.rows.length}`}
                  </span>
                </button>
                {!isCollapsed && <div>{group.rows.map(renderRow)}</div>}
              </div>
            );
          })}
    </div>
  );
}

function resolveOwnerName(
  row: BlockedInboxIssueRow,
  agentNameById: ReadonlyMap<string, string>,
  userLabelById: ReadonlyMap<string, string>,
): { label: string | null; isAgent: boolean } {
  const owner = row.attention.owner;
  if (owner.label) return { label: owner.label, isAgent: owner.type === "agent" };
  if (owner.agentId) {
    return { label: agentNameById.get(owner.agentId) ?? null, isAgent: true };
  }
  if (owner.userId) {
    return { label: userLabelById.get(owner.userId) ?? null, isAgent: false };
  }
  return { label: null, isAgent: false };
}

function BlockedTaskRow({
  row,
  agentNameById,
  userLabelById,
}: {
  row: BlockedInboxIssueRow;
  agentNameById: ReadonlyMap<string, string>;
  userLabelById: ReadonlyMap<string, string>;
}) {
  const { label: ownerName, isAgent } = resolveOwnerName(row, agentNameById, userLabelById);
  const stoppedAge = formatStoppedAge(row.attention.stoppedSinceAt);

  const desktopTrailing = (
    <span className="flex shrink-0 items-center gap-3 text-caption">
      <span
        className="hidden w-44 shrink-0 justify-start sm:inline-flex"
        data-testid="blocked-row-reason-column"
      >
        <BlockedReasonChip
          reason={row.attention.reason}
          severity={row.attention.severity}
          className="max-w-full"
        />
      </span>
      {ownerName ? (
        <span className="hidden w-36 min-w-0 items-center text-[var(--midnight-muted)] sm:inline-flex">
          <Identity name={ownerName} size="xs" className="max-w-full" />
        </span>
      ) : (
        <span className="hidden w-36 shrink-0 sm:inline-flex" aria-hidden="true" />
      )}
      <span
        className="hidden w-24 text-right text-[var(--midnight-muted)] sm:inline"
        data-testid="blocked-row-age"
      >
        {stoppedAge}
      </span>
    </span>
  );

  const mobileMeta = (
    <span className="flex flex-wrap items-center gap-x-1.5 gap-y-1">
      <span data-testid="blocked-row-age-mobile">{stoppedAge}</span>
      {ownerName ? (
        <>
          <span aria-hidden="true">·</span>
          <span
            className={cn(isAgent ? "font-medium text-[var(--midnight-soft)]" : null)}
            data-testid="blocked-row-owner-mobile"
          >
            {ownerName}
          </span>
        </>
      ) : null}
    </span>
  );

  return (
    <IssueRow
      issue={row.issue}
      titleSuffix={
        <BlockedReasonChip
          reason={row.attention.reason}
          severity={row.attention.severity}
          className="ml-2 max-w-48 align-middle sm:hidden"
        />
      }
      mobileMeta={mobileMeta}
      desktopTrailing={desktopTrailing}
    />
  );
}
