// apps/frontend/src/components/teams/inbox/InboxToolbar.tsx

// Ported from upstream Paperclip's pages/Inbox.tsx toolbar block
// (paperclip/ui/src/pages/Inbox.tsx:1874-2056) (MIT, (c) 2025 Paperclip AI).
// Tabs + search + filters + mark-all-read. Drops nesting toggle, group-by
// popover, and column picker.
//
// The search/filters/actions cluster composes the editorial atom library —
// `PanelToolbar` for the search-row scaffold, `EditorialButton` for the
// primary "New issue" CTA + secondary "Mark all as read" trigger. Tabs
// retain their bespoke pill styling (does not match `PanelTabs`'s underline
// look) and stay as raw <button role="tab"> elements.

"use client";

import { useMemo } from "react";
import { Plus } from "lucide-react";
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
import { EditorialButton, PanelToolbar } from "@/components/teams/shell";
import { IssueFiltersPopover } from "@/components/teams/inbox/IssueFiltersPopover";
import { ViewModeToggle } from "@/components/teams/inbox/ViewModeToggle";
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

  viewMode: "list" | "board";
  onViewModeChange: (mode: "list" | "board") => void;
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
    viewMode,
    onViewModeChange,
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

  // Filters slot — IssueFiltersPopover trigger (iconOnly).
  // Hoisted out of the JSX for readability — desktop is the only consumer
  // today (the mobile PanelToolbar renders search-only). If mobile gains an
  // actions/filters row, both branches can share this single source of truth.
  const filtersSlot = (
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
  );

  // Actions slot — view-mode toggle + primary "+ New issue" + secondary
  // "Mark all as read" wrapped in the AlertDialog confirmation flow.
  const actionsSlot = (
    <>
      <ViewModeToggle value={viewMode} onChange={onViewModeChange} />

      <EditorialButton
        type="button"
        variant="primary"
        size="sm"
        onClick={onNewIssue}
        className="shrink-0"
      >
        <Plus className="h-3 w-3" />
        <span>New issue</span>
      </EditorialButton>

      <AlertDialog>
        <AlertDialogTrigger asChild>
          <EditorialButton
            type="button"
            variant="secondary"
            size="sm"
            className="shrink-0"
            disabled={markDisabled}
            title={
              markDisabled
                ? "No unread items"
                : `Mark all ${unreadCount} unread as read`
            }
          >
            Mark all as read
          </EditorialButton>
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
    </>
  );

  return (
    <div className="space-y-2">
      {/* Mobile-only search row — search input only, mirroring the
          original layout. Filter trigger + primary actions are deferred
          until the desktop breakpoint. */}
      <div className="sm:hidden">
        <PanelToolbar
          searchValue={searchQuery}
          onSearchChange={onSearchChange}
          searchPlaceholder="Search inbox..."
          searchAriaLabel="Search inbox"
        />
      </div>

      {/* Desktop toolbar row — tabs sit inline on the left, PanelToolbar
          (search + filters + actions) takes the remaining width on the
          right. */}
      <div className="hidden flex-wrap items-center gap-3 sm:flex">
        {/* Tab bar — bespoke pill styling, intentionally does not use
            PanelTabs (which renders an underline-style tab list).
            TODO(teams-ui-parity): migrate to PanelTabs once that atom
            supports a pill variant matching this forest-tint active style. */}
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

        <PanelToolbar
          className="flex-1"
          searchValue={searchQuery}
          onSearchChange={onSearchChange}
          searchPlaceholder="Search inbox..."
          searchAriaLabel="Search inbox"
          filters={filtersSlot}
          actions={actionsSlot}
        />
      </div>
    </div>
  );
}
