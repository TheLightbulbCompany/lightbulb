// apps/frontend/src/components/teams/inbox/InboxToolbar.tsx

// Ported from upstream Paperclip's pages/Inbox.tsx toolbar block
// (paperclip/ui/src/pages/Inbox.tsx) (MIT, (c) 2025 Paperclip AI).
// Tabs + search + filters + mark-all-read. Drops nesting toggle, group-by
// popover, and column picker.
//
// The search/filters/actions cluster composes the editorial atom library —
// `PanelToolbar` for the search-row scaffold, `EditorialButton` for the
// primary "New task" CTA + secondary "Mark all as read" trigger. Tabs
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

  /** Fired when the user clicks the primary "New task" button. */
  onNewIssue: () => void;

  /**
   * Decisions waiting on this human, badged on the "Needs you" tab.
   *
   * This is what makes the surface findable — the tab bar is on screen no
   * matter which tab is active, so the number is always visible. Pass 0 (or
   * omit) to render no badge: an absent count must never be drawn as a
   * hopeful zero, and a fabricated number here would be worse than none.
   */
  attentionCount?: number;
}

// "Needs you" leads deliberately. It is the only tab whose contents are
// blocking someone, and the owner's complaint was that approvals were
// findable only by knowing where to look — so it sits where the eye starts,
// carrying a count.
const TABS: { value: InboxTab; label: string }[] = [
  { value: "approvals", label: "Needs you" },
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
    attentionCount = 0,
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

  // Actions slot — primary "+ New task" + secondary "Mark all as read"
  // wrapped in the AlertDialog confirmation flow. The Inbox is list-only
  // (the kanban board lives on the Tasks page), so there is no view-mode
  // toggle here.
  const actionsSlot = (
    <>
      <EditorialButton
        type="button"
        variant="primary"
        size="sm"
        onClick={onNewIssue}
        className="shrink-0"
      >
        <Plus className="h-3 w-3" />
        <span>New task</span>
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

  // Tab bar — bespoke pill styling, intentionally does not use PanelTabs
  // (which renders an underline-style tab list).
  // TODO(teams-ui-parity): migrate to PanelTabs once that atom supports a
  // pill variant matching this forest-tint active style.
  //
  // Promoted to its own always-visible row (it used to sit inline inside the
  // desktop-only toolbar row). Mobile had NO tab bar at all, which was
  // survivable while every tab was a view of the same issue list — but the
  // "Needs you" tab is the only place pending approvals are listed, so
  // leaving the bar desktop-only would put them out of reach on a phone.
  // Scrolls horizontally rather than wrapping so the leading tab, which is
  // the one carrying a count, is always the one you see first.
  const tabsSlot = (
    <div
      role="tablist"
      aria-label="Inbox tabs"
      className="inline-flex max-w-full items-center gap-1 overflow-x-auto rounded-md p-0.5"
      style={{ background: "var(--cream-deeper)" }}
    >
      {TABS.map((t) => {
        const isActive = tab === t.value;
        const badge = t.value === "approvals" ? attentionCount : 0;
        return (
          <button
            key={t.value}
            type="button"
            role="tab"
            aria-selected={isActive}
            onClick={() => onTabChange(t.value)}
            className={cn(
              "flex h-7 shrink-0 items-center gap-1.5 rounded-sm px-3 text-xs font-medium transition-colors",
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
            {badge > 0 && (
              <span
                // Forest, not red: these are answers owed, not incidents. The
                // urgency of an individual decision is carried by its age in
                // the list, not by shouting on the tab.
                className="rounded-full px-1.5 text-[10px] font-semibold leading-4 tabular-nums"
                style={{ background: "var(--forest)", color: "var(--cream)" }}
              >
                {badge}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );

  return (
    <div className="space-y-2">
      {tabsSlot}

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

      {/* Desktop toolbar row — PanelToolbar (search + filters + actions)
          takes the full width beneath the tab bar. */}
      <div className="hidden flex-wrap items-center gap-3 sm:flex">
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
