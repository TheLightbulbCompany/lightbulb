// apps/frontend/src/components/teams/inbox/InboxPage.tsx

// Ported from upstream Paperclip's pages/Inbox.tsx (assembly + state mgmt)
// (paperclip/ui/src/pages/Inbox.tsx) (MIT, (c) 2025 Paperclip AI).
// Composes Tasks 1-10 outputs (lib + 4 hooks + 3 components).
// See spec at docs/superpowers/specs/2026-05-04-teams-inbox-deep-port-design.md

"use client";

import { useCallback, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import dynamic from "next/dynamic";

import { Inbox as InboxIcon } from "lucide-react";

import { InboxToolbar } from "@/components/teams/inbox/InboxToolbar";
import { InboxList } from "@/components/teams/inbox/InboxList";
import { PageSkeleton } from "@/components/teams/shared/components/PageSkeleton";
import { NewIssueDialog } from "@/components/teams/issues/NewIssueDialog";
import { PanelEmptyState, PanelMasthead } from "@/components/teams/shell";

import { useInboxData } from "@/components/teams/inbox/hooks/useInboxData";
import { useInboxKeyboardNav } from "@/components/teams/inbox/hooks/useInboxKeyboardNav";
import { useInboxArchiveStack } from "@/components/teams/inbox/hooks/useInboxArchiveStack";
import { useReadInboxItems } from "@/components/teams/inbox/hooks/useReadInboxItems";
import { useInboxViewMode } from "@/components/teams/inbox/hooks/useInboxViewMode";
import { useIssueMutations } from "@/components/teams/issues/hooks/useIssueMutations";
import type { IssueUpdateInput } from "@/components/teams/shared/types";

// Lazy-load the board so the dnd-kit + KanbanColumn/KanbanCard tree only
// arrives in the client bundle when the user actually flips to board view.
const KanbanBoard = dynamic(
  () =>
    import("@/components/teams/inbox/KanbanBoard").then((m) => m.KanbanBoard),
  {
    ssr: false,
    loading: () => (
      <div className="p-4" style={{ color: "var(--mute)" }}>
        Loading board…
      </div>
    ),
  },
);

import {
  buildGroupedInboxSections,
  buildInboxKeyboardNavEntries,
  getInboxWorkItems,
  getRecentTouchedIssues,
  matchesInboxIssueSearch,
  type InboxKeyboardNavEntry,
} from "@/components/teams/shared/lib/inbox";
import {
  defaultIssueFilterState,
  type IssueFilterState,
} from "@/components/teams/shared/lib/issueFilters";
import {
  loadLastInboxTab,
  saveLastInboxTab,
} from "@/components/teams/shared/lib/inboxStorage";
import type { InboxTab } from "@/components/teams/shared/queryKeys";
import type {
  CompanyAgent,
  CompanyMember,
  Issue,
  IssueLabel,
  IssueProject,
} from "@/components/teams/shared/types";

const KNOWN_INBOX_TABS = new Set<InboxTab>([
  "mine",
  "recent",
  "all",
  "unread",
  "approvals",
  "runs",
  "joins",
]);

function isInboxTab(value: string | null): value is InboxTab {
  return value !== null && KNOWN_INBOX_TABS.has(value as InboxTab);
}

export interface InboxPageProps {
  /** Owner id used as the localStorage namespace + cross-tab `storage` key. */
  companyId: string;
  /** Currently signed-in user id; passed through to IssueFiltersPopover. */
  currentUserId: string;
  /** Optional metadata used by the filters popover; safe to default to []. */
  agents?: CompanyAgent[];
  members?: CompanyMember[];
  projects?: IssueProject[];
  labels?: IssueLabel[];
}

/**
 * Top-level Inbox page: assembles toolbar + list + skeleton with the four
 * inbox hooks. Responsible for tab/search/filter/selection state, URL sync,
 * keyboard nav wiring, and empty-state copy.
 */
export function InboxPage({
  companyId,
  currentUserId,
  agents = [],
  members = [],
  projects = [],
  labels = [],
}: InboxPageProps) {
  const router = useRouter();
  const searchParams = useSearchParams();

  // 1. URL-synced tab. The URL wins on first render, falling back to
  //    localStorage so a fresh `/teams/inbox` link still honors the user's
  //    last tab.
  const urlTabParam = searchParams.get("tab");
  const [tab, setTabState] = useState<InboxTab>(() =>
    isInboxTab(urlTabParam) ? urlTabParam : loadLastInboxTab(companyId),
  );

  const setTab = useCallback(
    (next: InboxTab) => {
      setTabState(next);
      saveLastInboxTab(companyId, next);
      const params = new URLSearchParams(searchParams.toString());
      params.set("tab", next);
      router.replace(`?${params.toString()}`, { scroll: false });
    },
    [companyId, router, searchParams],
  );

  // 2. Search + filter state.
  const [searchQuery, setSearchQuery] = useState("");
  const [filterState, setFilterState] =
    useState<IssueFilterState>(defaultIssueFilterState);

  // 3. Selection state (single id; null = no selection).
  const [selectedIssueId, setSelectedIssueId] = useState<string | null>(null);

  // 3b. NewIssueDialog open state.
  const [newIssueOpen, setNewIssueOpen] = useState(false);

  // 4. Data + side-effect hooks.
  const { mineIssues, touchedIssues, allIssues, isLoading, isError } =
    useInboxData();
  const archiveStack = useInboxArchiveStack();
  const readItems = useReadInboxItems(companyId);
  const { viewMode, setViewMode } = useInboxViewMode(companyId ?? null);
  const { update: updateIssue } = useIssueMutations();
  const onUpdateIssue = useCallback(
    (id: string, data: Record<string, unknown>) => {
      // Route through useIssueMutations so the SWR keys for /teams/inbox?...
      // and the issue detail are invalidated on success. Without this the
      // board re-renders from stale filteredIssues and the dragged card
      // snaps back to its old column even when the PATCH succeeded.
      updateIssue(id, data as IssueUpdateInput).catch((err) => {
        console.error(`Failed to update issue ${id}:`, err);
      });
    },
    [updateIssue],
  );

  // 5. Compute the active issue list per tab.
  const activeIssues = useMemo<Issue[]>(() => {
    if (tab === "mine") return mineIssues;
    if (tab === "recent")
      return getRecentTouchedIssues(touchedIssues, { unreadOnly: false });
    if (tab === "unread")
      return getRecentTouchedIssues(touchedIssues, { unreadOnly: true });
    return allIssues;
  }, [tab, mineIssues, touchedIssues, allIssues]);

  // 6. Client-side search filtering (BFF returns the unfiltered list per tab).
  const filteredIssues = useMemo(
    () =>
      activeIssues.filter((issue) =>
        matchesInboxIssueSearch(issue, searchQuery),
      ),
    [activeIssues, searchQuery],
  );

  // 7. Build the today/earlier (or search) sections.
  const sections = useMemo(
    () =>
      buildGroupedInboxSections(getInboxWorkItems(filteredIssues), {
        searchQuery,
        nowIso: new Date().toISOString(),
      }),
    [filteredIssues, searchQuery],
  );

  // 8. Flat keyboard-nav projection.
  const navItems = useMemo<InboxKeyboardNavEntry[]>(
    () => buildInboxKeyboardNavEntries(sections),
    [sections],
  );

  // 9. Selection clamp — when sections change (e.g. after archive), drop
  //    the selected id if it's no longer present. Doing this during render
  //    (vs. inside an effect) avoids the cascading-render lint and matches
  //    the pattern used by `useReadInboxItems` for prop-derived state.
  //    See https://react.dev/learn/you-might-not-need-an-effect#adjusting-some-state-when-a-prop-changes
  const selectedIndex = selectedIssueId
    ? navItems.findIndex((item) => item.id === selectedIssueId)
    : -1;
  if (selectedIssueId && selectedIndex === -1) {
    // Stale id — drop it during render. setState during render is the
    // documented React pattern for prop/derived-state desync.
    setSelectedIssueId(null);
  }

  // 10. Wire keyboard shortcuts. Upstream allows j/k/Arrow/Enter navigation
  //     on every tab; only archive-class shortcuts (a/y/r/u) are restricted
  //     to the "mine" tab.
  useInboxKeyboardNav({
    enableNav: true,
    enableArchive: tab === "mine",
    navItems,
    selectedIndex,
    onSelectIndex: (idx) => {
      const item = navItems[idx];
      setSelectedIssueId(item?.id ?? null);
    },
    onOpen: (item) => {
      router.push(`/teams/issues/${item.id}`);
    },
    onArchive: (item) => {
      void archiveStack.archive(item.id);
    },
    onMarkRead: (item) => {
      void archiveStack.markRead(item.id);
      readItems.markRead(item.id);
    },
    onUndoArchive: () => {
      void archiveStack.undoArchive();
    },
    hasUndoableArchive: archiveStack.hasUndoableArchive,
  });

  // 11. Mark-all-read action.
  const unreadCount = useMemo(
    () => filteredIssues.filter((i) => i.unread).length,
    [filteredIssues],
  );
  const handleMarkAllRead = useCallback(() => {
    const ids = filteredIssues.filter((i) => i.unread).map((i) => i.id);
    readItems.markManyRead(ids);
    // Server sync intentionally deferred — the optimistic local set is what
    // the badge + IssueRow pip read. Per-issue mark-read POSTs still fire on
    // single-row interactions via archiveStack.markRead.
  }, [filteredIssues, readItems]);

  // 12. Empty-state copy per tab + per-search. Headline preserves the
  //     terse editorial voice from upstream; supporting text gives a single
  //     sentence of context appropriate to the tab.
  const emptyState = useMemo<{ headline: string; supporting: string }>(() => {
    if (searchQuery) {
      return {
        headline: "No matches.",
        supporting: "No inbox items match your search.",
      };
    }
    if (tab === "mine") {
      return {
        headline: "Inbox zero.",
        supporting:
          "Nothing pending right now. Items appear here when an agent completes a run, an issue is assigned to you, or an approval is requested.",
      };
    }
    if (tab === "unread") {
      return {
        headline: "No new inbox items.",
        supporting: "Everything in your inbox has been read.",
      };
    }
    if (tab === "recent") {
      return {
        headline: "No recent inbox items.",
        supporting: "Recently touched issues will surface here.",
      };
    }
    return {
      headline: "Nothing here.",
      supporting: "No inbox items match these filters.",
    };
  }, [tab, searchQuery]);

  return (
    <div className="flex flex-col p-4 sm:p-6 editorial-page">
      <PanelMasthead overline="Inbox" title="Inbox" className="mb-4" />
      <InboxToolbar
        tab={tab}
        onTabChange={setTab}
        searchQuery={searchQuery}
        onSearchChange={setSearchQuery}
        filterState={filterState}
        onFilterChange={setFilterState}
        agents={agents}
        members={members}
        projects={projects}
        labels={labels}
        currentUserId={currentUserId}
        unreadCount={unreadCount}
        onMarkAllRead={handleMarkAllRead}
        onNewIssue={() => setNewIssueOpen(true)}
        viewMode={viewMode}
        onViewModeChange={setViewMode}
      />
      {isError && (
        <div
          role="alert"
          className="mt-4 rounded-md border px-4 py-3 text-sm"
          style={{
            borderColor: "rgba(179, 36, 36, 0.25)",
            background: "rgba(179, 36, 36, 0.06)",
            color: "var(--danger)",
          }}
        >
          Failed to load inbox. Try refreshing.
        </div>
      )}
      {isLoading && sections.length === 0 ? (
        <div className="mt-4">
          <PageSkeleton variant="inbox" />
        </div>
      ) : viewMode === "board" ? (
        <KanbanBoard
          issues={filteredIssues}
          agents={agents}
          onUpdateIssue={onUpdateIssue}
        />
      ) : sections.length === 0 ? (
        <PanelEmptyState
          icon={InboxIcon}
          headline={emptyState.headline}
          supporting={emptyState.supporting}
        />
      ) : (
        <InboxList
          sections={sections}
          selectedIssueId={selectedIssueId}
          onSelect={setSelectedIssueId}
          onArchive={(id) => void archiveStack.archive(id)}
          onMarkRead={(id) => {
            void archiveStack.markRead(id);
            readItems.markRead(id);
          }}
          archivingIds={archiveStack.archivingIssueIds}
          searchQuery={searchQuery}
        />
      )}

      <NewIssueDialog
        open={newIssueOpen}
        onOpenChange={setNewIssueOpen}
        agents={agents}
        projects={projects}
        onCreated={(issueId) => {
          setNewIssueOpen(false);
          router.push(`/teams/issues/${issueId}`);
        }}
      />
    </div>
  );
}
