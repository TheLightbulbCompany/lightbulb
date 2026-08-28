// apps/frontend/src/components/teams/inbox/InboxPage.tsx

// Ported from upstream Paperclip's pages/Inbox.tsx (assembly + state mgmt)
// (paperclip/ui/src/pages/Inbox.tsx) (MIT, (c) 2025 Paperclip AI).

"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { Inbox as InboxIcon } from "lucide-react";

import { InboxToolbar } from "@/components/teams/inbox/InboxToolbar";
import { InboxList } from "@/components/teams/inbox/InboxList";
import { AttentionList } from "@/components/teams/inbox/AttentionList";
import { NeedsAttentionBand } from "@/components/teams/inbox/NeedsAttentionBand";
import { PageSkeleton } from "@/components/teams/shared/components/PageSkeleton";
import { NewIssueDialog } from "@/components/teams/issues/NewIssueDialog";
import { PanelEmptyState, PanelMasthead } from "@/components/teams/shell";
import {
  ONBOARDING_FIRST_TASK_TITLE,
  ONBOARDING_FIRST_TASK_DESCRIPTION,
  resolveCeoAgentId,
} from "@/components/getstarted/getStartedFirstTask";

import { useInlineTeamsNav } from "@/components/chat/InlineTeamsNav";
import { teamsDetailUrl } from "@/lib/shellNav";
import { useInboxData } from "@/components/teams/inbox/hooks/useInboxData";
import { useAttentionFeed } from "@/components/teams/inbox/hooks/useAttentionFeed";
import { useInboxKeyboardNav } from "@/components/teams/inbox/hooks/useInboxKeyboardNav";
import { useInboxArchiveStack } from "@/components/teams/inbox/hooks/useInboxArchiveStack";
import { useReadInboxItems } from "@/components/teams/inbox/hooks/useReadInboxItems";

import {
  buildGroupedInboxSections,
  buildInboxKeyboardNavEntries,
  getInboxWorkItems,
  getRecentTouchedIssues,
  matchesInboxIssueSearch,
  type InboxKeyboardNavEntry,
} from "@/components/teams/shared/lib/inbox";
import {
  applyIssueFilters,
  defaultIssueFilterState,
  type IssueFilterState,
} from "@/components/teams/shared/lib/issueFilters";
import { useLiveRuns } from "@/components/teams/dashboard/hooks/useLiveRuns";
import { buildLiveIssueState } from "@/components/teams/shared/lib/liveRuns";
import {
  isInboxTab,
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
  /**
   * One-shot deep-link intent to pop the create dialog open: "1" = blank
   * composer, "onboarding" = the Get Started first-task step. Threaded by the
   * shell from its own channel (URL param on mobile). null = no intent.
   */
  newIssue?: "1" | "onboarding" | null;
  /** Fired once the intent is honored so the shell strips its channel. */
  onNewIssueConsumed?: () => void;
  /**
   * ?tab deep link (bookmarks/shared links from when tab clicks wrote the
   * URL). Read-side only: seeds/overrides the active tab when it names a
   * known tab; tab clicks still persist ONLY to localStorage.
   */
  initialTab?: string | null;
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
  newIssue = null,
  onNewIssueConsumed,
  initialTab = null,
}: InboxPageProps) {
  // Inside the /chat shell a nav provider is present → open the issue detail
  // in-shell. Without one (bare mount), fall back to committing the canonical
  // in-shell URL directly — same destination, no provider hop.
  const inlineNav = useInlineTeamsNav();
  const openIssue = useCallback(
    (issueId: string) => {
      if (inlineNav) {
        inlineNav.openDetail("issue-detail", issueId);
        return;
      }
      window.history.pushState(null, "", teamsDetailUrl("issue", issueId));
    },
    [inlineNav],
  );

  // 1. Active tab: a validated ?tab deep link wins on first render, else the
  //    per-owner localStorage last-tab. (Tab clicks are no longer mirrored to
  //    the URL — they persist only to localStorage — but inbound deep links
  //    are still honored: real bookmarks carry ?tab from when clicks wrote
  //    it.) A re-delivered deep link on an already-mounted panel re-applies
  //    via the render-time prev-compare below.
  const [tab, setTabState] = useState<InboxTab>(() =>
    isInboxTab(initialTab) ? initialTab : loadLastInboxTab(companyId),
  );
  const [prevInitialTab, setPrevInitialTab] = useState(initialTab);
  if (initialTab !== prevInitialTab) {
    setPrevInitialTab(initialTab);
    if (isInboxTab(initialTab)) setTabState(initialTab);
  }

  const setTab = useCallback(
    (next: InboxTab) => {
      setTabState(next);
      saveLastInboxTab(companyId, next);
    },
    [companyId],
  );

  // 2. Search + filter state.
  const [searchQuery, setSearchQuery] = useState("");
  const [filterState, setFilterState] =
    useState<IssueFilterState>(defaultIssueFilterState);

  // 3. Selection state (single id; null = no selection).
  const [selectedIssueId, setSelectedIssueId] = useState<string | null>(null);

  // 3b. NewIssueDialog open state.
  //
  // Deep-link entry: the shell hands down a one-shot `newIssue` intent to pop
  // the dialog straight open. "1" = blank composer; "onboarding" = the Get
  // Started first-task step, which pre-fills a CEO-directed task. Opening is done
  // WITHOUT an effect-driven setState (the lint rule forbids it, and it would
  // cascade renders): the open state is lazily seeded from the prop on first
  // render, and a later re-delivery (the same intent fired again while this panel
  // is mounted) is detected by comparing the prop against its previous value
  // DURING render — React's "adjust state when a prop changes" pattern, the same
  // one the selection clamp below uses. The effect then ONLY reports consumption
  // so the shell strips its channel (not setState), keeping a refresh / back-nav
  // from reopening it. The prefill intent is captured as STATE — not re-derived
  // from the prop — so it survives the strip and any later re-open.
  const [newIssueOpen, setNewIssueOpen] = useState(
    () => newIssue === "1" || newIssue === "onboarding",
  );
  const [newIssuePrefill, setNewIssuePrefill] = useState(
    () => newIssue === "onboarding",
  );
  const [prevNewIssue, setPrevNewIssue] = useState(newIssue);
  if (newIssue !== prevNewIssue) {
    setPrevNewIssue(newIssue);
    if (newIssue === "1" || newIssue === "onboarding") {
      setNewIssueOpen(true);
      setNewIssuePrefill(newIssue === "onboarding");
    }
  }

  useEffect(() => {
    if (newIssue !== "1" && newIssue !== "onboarding") return;
    onNewIssueConsumed?.();
  }, [newIssue, onNewIssueConsumed]);

  // 4. Data + side-effect hooks.
  //
  // The "Needs you" tab renders its OWN list from the unified attention feed,
  // so the three issue reads are gated off while it is active — that tab
  // never consults the issue list (which silently caps around 120 rows,
  // isol8 #1992; a decision backlog must not be derived from a capped scan).
  const isAttentionTab = tab === "approvals";
  const { mineIssues, touchedIssues, allIssues, isLoading, isError } =
    useInboxData({ enabled: !isAttentionTab });
  // Read here as well as inside AttentionList: SWR dedupes on the shared key,
  // so the badge and the list are one request and can never disagree.
  const attention = useAttentionFeed();
  const archiveStack = useInboxArchiveStack();
  const readItems = useReadInboxItems(companyId);
  // Per-issue live-run state (running / stalled + tooltip) drives BOTH the
  // per-row live indicator (`IssueRow.live`) and the filters popover's "Live
  // runs only" facet. The shared fold picks the most severe silence signal
  // per issue (critical > suspicious > healthy) so a hung run is never
  // masked by a healthy or milder-stalled one.
  const { runs: liveRuns } = useLiveRuns();
  const liveIssueState = useMemo(() => buildLiveIssueState(liveRuns), [liveRuns]);
  // Set view kept for the shared filter predicate (membership-only).
  const liveIssueIds = useMemo(
    () => new Set(liveIssueState.keys()),
    [liveIssueState],
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

  // 6. Client-side filtering (BFF returns the unfiltered list per tab):
  //    structured facets (the popover) THEN free-text search. The facet pass
  //    uses the shared `applyIssueFilters` predicate so the Inbox and the
  //    Tasks surface never drift apart.
  const filteredIssues = useMemo(() => {
    const facetFiltered = applyIssueFilters(activeIssues, filterState, {
      currentUserId,
      liveIssueIds,
    });
    return facetFiltered.filter((issue) =>
      matchesInboxIssueSearch(issue, searchQuery),
    );
  }, [activeIssues, filterState, currentUserId, liveIssueIds, searchQuery]);

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
  //     to the "mine" tab. The Inbox is list-only, so nav is always enabled.
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
      openIssue(item.id);
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
    <div className="flex flex-col h-full min-h-0 p-4 sm:p-6 editorial-page">
      <PanelMasthead overline="Inbox" title="Inbox" className="mb-4" />
      <NeedsAttentionBand companyId={companyId} agents={agents} />
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
        attentionCount={attention.count}
      />
      {isError && !isAttentionTab && (
        <div
          role="alert"
          className="mt-4 rounded-md border px-4 py-3 text-sm"
          style={{
            borderColor: "color-mix(in srgb, var(--err) 25%, transparent)",
            background: "color-mix(in srgb, var(--err) 6%, transparent)",
            color: "var(--err)",
          }}
        >
          Failed to load inbox. Try refreshing.
        </div>
      )}
      <div className="mt-4 flex flex-1 flex-col min-h-0">
        {isAttentionTab ? (
          <div className="h-full overflow-y-auto">
            <AttentionList
              agents={agents}
              currentUserId={currentUserId}
              onOpenIssue={openIssue}
            />
          </div>
        ) : isLoading && sections.length === 0 ? (
          <PageSkeleton variant="inbox" />
        ) : sections.length === 0 ? (
          <PanelEmptyState
            icon={InboxIcon}
            headline={emptyState.headline}
            supporting={emptyState.supporting}
          />
        ) : (
          <div className="h-full overflow-y-auto">
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
              liveIssueState={liveIssueState}
              searchQuery={searchQuery}
            />
          </div>
        )}
      </div>

      <NewIssueDialog
        key={newIssuePrefill ? "onboarding" : "blank"}
        open={newIssueOpen}
        onOpenChange={(next) => {
          setNewIssueOpen(next);
          if (!next) setNewIssuePrefill(false);
        }}
        agents={agents}
        projects={projects}
        defaultTitle={newIssuePrefill ? ONBOARDING_FIRST_TASK_TITLE : undefined}
        defaultDescription={
          newIssuePrefill ? ONBOARDING_FIRST_TASK_DESCRIPTION : undefined
        }
        defaultAssigneeAgentId={
          newIssuePrefill ? resolveCeoAgentId(agents) : undefined
        }
        defaultStatus={newIssuePrefill ? "todo" : undefined}
        onCreated={(issueId) => {
          setNewIssueOpen(false);
          setNewIssuePrefill(false);
          openIssue(issueId);
        }}
      />
    </div>
  );
}
