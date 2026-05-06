// apps/frontend/src/components/teams/inbox/InboxToolbar.tsx

// Ported from upstream Paperclip's pages/Inbox.tsx toolbar block
// (paperclip/ui/src/pages/Inbox.tsx:1874-2056) (MIT, (c) 2025 Paperclip AI).
// v1: tabs + search + filters + mark-all-read. Drops nesting toggle, group-by
// popover, and column picker (deferred per #3c plan).
// See spec at docs/superpowers/specs/2026-05-04-teams-inbox-deep-port-design.md

"use client";

import { useMemo } from "react";
import { Plus, Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { cn } from "@/lib/utils";
import { IssueFiltersPopover } from "@/components/teams/inbox/IssueFiltersPopover";
import type { InboxTab } from "@/components/teams/shared/queryKeys";
import type { IssueFilterState } from "@/components/teams/shared/lib/issueFilters";
import type {
  CompanyAgent,
  CompanyMember,
  IssueLabel,
  IssueProject,
} from "@/components/teams/shared/types";

export interface InboxToolbarProps {
  tab: InboxTab;
  onTabChange: (tab: InboxTab) => void;

  searchQuery: string;
  onSearchChange: (q: string) => void;

  filterState: IssueFilterState;
  onFilterChange: (s: IssueFilterState) => void;

  agents: CompanyAgent[];
  members: CompanyMember[];
  projects: IssueProject[];
  labels: IssueLabel[];
  currentUserId: string;

  unreadCount: number;
  onMarkAllRead: () => void;

  /** Fired when the user clicks the primary "New issue" button. */
  onNewIssue: () => void;
}

const TABS: { value: InboxTab; label: string }[] = [
  { value: "mine", label: "Mine" },
  { value: "recent", label: "Recent" },
  { value: "unread", label: "Unread" },
  { value: "all", label: "All" },
];

function countActiveFilters(state: IssueFilterState): number {
  let count = 0;
  count += state.statuses.length;
  count += state.priorities.length;
  count += state.assignees.length;
  count += state.creators.length;
  count += state.labels.length;
  count += state.projects.length;
  count += state.workspaces.length;
  if (state.liveOnly) count += 1;
  if (state.hideRoutineExecutions) count += 1;
  return count;
}

export function InboxToolbar(props: InboxToolbarProps) {
  const {
    tab,
    onTabChange,
    searchQuery,
    onSearchChange,
    filterState,
    onFilterChange,
    agents,
    members,
    projects,
    labels,
    currentUserId,
    unreadCount,
    onMarkAllRead,
    onNewIssue,
  } = props;

  const activeFilterCount = useMemo(() => countActiveFilters(filterState), [filterState]);

  // IssueFiltersPopover takes Partial<IssueFilterState> patches; merge here so
  // the toolbar's public API exposes the simpler "full state replacement" shape.
  const handleFilterPatch = (patch: Partial<IssueFilterState>): void => {
    onFilterChange({ ...filterState, ...patch });
  };

  // Map CompanyMember -> creators option list (kind: "user").
  const creatorOptions = useMemo(
    () =>
      members.map((m) => ({
        id: `user:${m.userId}`,
        label: m.name ?? m.email ?? m.userId,
        kind: "user" as const,
        searchText: `${m.name ?? ""} ${m.email ?? ""}`.trim(),
      })),
    [members],
  );

  const projectOptions = useMemo(
    () => projects.map((p) => ({ id: p.id, name: p.name })),
    [projects],
  );

  const labelOptions = useMemo(
    () => labels.map((l) => ({ id: l.id, name: l.name, color: l.color ?? "" })),
    [labels],
  );

  const agentOptions = useMemo(
    () => agents.map((a) => ({ id: a.id, name: a.name })),
    [agents],
  );

  const markDisabled = unreadCount === 0;

  return (
    <div className="space-y-2">
      {/* Mobile-only search row */}
      <div className="relative sm:hidden">
        <Search
          className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2"
          style={{ color: "var(--mute)" }}
        />
        <Input
          type="search"
          placeholder="Search inbox..."
          value={searchQuery}
          onChange={(e) => onSearchChange(e.target.value)}
          className="h-8 w-full pl-8 text-xs"
          data-page-search
        />
      </div>

      {/* Desktop toolbar row */}
      <div className="hidden flex-wrap items-center justify-between gap-2 sm:flex">
        {/* Tab bar — plain styled buttons (no shadcn Tabs primitive vendored). */}
        {/* Retheme: prior amber-700 active swap → forest editorial token. */}
        <div
          role="tablist"
          aria-label="Inbox tabs"
          className="inline-flex items-center gap-1 rounded-md p-0.5"
          style={{ background: "var(--cream-deeper)" }}
        >
          {TABS.map((t) => {
            const isActive = tab === t.value;
            return (
              <button
                key={t.value}
                type="button"
                role="tab"
                aria-selected={isActive}
                onClick={() => onTabChange(t.value)}
                className={cn(
                  "h-7 rounded-sm px-3 text-xs font-medium transition-colors",
                )}
                style={
                  isActive
                    ? {
                        background: "var(--forest-tint)",
                        color: "var(--forest)",
                      }
                    : { color: "var(--mute)" }
                }
              >
                {t.label}
              </button>
            );
          })}
        </div>

        <div className="flex flex-1 items-center justify-end gap-2">
          {/* Desktop search input */}
          <div className="relative">
            <Search
              className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2"
              style={{ color: "var(--mute)" }}
            />
            <Input
              type="search"
              placeholder="Search inbox..."
              value={searchQuery}
              onChange={(e) => onSearchChange(e.target.value)}
              className="h-8 w-[220px] pl-8 text-xs"
              data-page-search
            />
          </div>

          <IssueFiltersPopover
            state={filterState}
            onChange={handleFilterPatch}
            activeFilterCount={activeFilterCount}
            agents={agentOptions}
            creators={creatorOptions}
            projects={projectOptions}
            labels={labelOptions}
            currentUserId={currentUserId}
            buttonVariant="outline"
            iconOnly
          />

          {/* Primary action: open the New Issue dialog. */}
          <button
            type="button"
            onClick={onNewIssue}
            className="editorial-btn editorial-btn--primary editorial-btn--sm shrink-0"
          >
            <Plus className="h-3 w-3" />
            <span>New issue</span>
          </button>

          {/* Mark all as read with confirmation dialog. */}
          <AlertDialog>
            <AlertDialogTrigger asChild>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="editorial-btn editorial-btn--secondary editorial-btn--sm shrink-0"
                disabled={markDisabled}
                title={
                  markDisabled
                    ? "No unread items"
                    : `Mark all ${unreadCount} unread as read`
                }
              >
                Mark all as read
              </Button>
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Mark all as read?</AlertDialogTitle>
                <AlertDialogDescription>
                  This will mark {unreadCount} unread{" "}
                  {unreadCount === 1 ? "item" : "items"} as read.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Cancel</AlertDialogCancel>
                <AlertDialogAction onClick={onMarkAllRead}>
                  Confirm
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </div>
      </div>
    </div>
  );
}
