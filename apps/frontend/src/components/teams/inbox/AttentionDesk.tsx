// apps/frontend/src/components/teams/inbox/AttentionDesk.tsx

// Ported from upstream Paperclip's WhatNeedsMe page body
// (paperclip/ui/src/pages/WhatNeedsMe.tsx @ v2026.1012.0-isol8.4)
// (MIT, (c) 2025 Paperclip AI). The feed read (dismissed rows included, whole
// desk), the optimistic hide / restore reset on each fresh feed, the server
// clock as "now", the desk / Aging split, the default shelves (Decide now /
// New today / Earlier), the Snoozed / Dismissed / Aging curtains, the dismiss
// toast with Undo (~8s), the auto-expand of the topmost inline decision, the
// bundle header and the zero / caught-up copy are verbatim, except where listed.
//
// Adaptations vs upstream (only what had to change):
//   - mounted as Activity -> For you -> "Needs you", not a /decisions page: no
//     h1, breadcrumbs or PageSkeleton
//   - react-query feed read  -> useTeamsApi.read of the BFF's
//                               /attention?includeDismissed=true&all=true (a key
//                               apart from the badge's /attention)
//   - agents / session reads -> the caller's agents and currentUserId
//   - pushToast              -> notifySuccess with an Undo action
//   - `hideFailedRuns`       -> under For you the Inbox's failed-run groups (with
//                               Retry) list failures, so the desk leaves its own out
//   - not ported: DecisionsToolbar (group / sort / filter), DecisionQueueRail and
//     DecisionDateChips (decision queues and the date chips' chrome), the
//     Decided / Expired history curtains, the `?decisionId=` deep link,
//     j / k / e / x keyboard shortcuts and the incremental row budget
//   - palette classes        -> tokens

"use client";

import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { CheckCircle2, Inbox } from "lucide-react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { notifySuccess } from "@/lib/notify";
import { teamsQueryKeys } from "@/components/teams/shared/queryKeys";
import { useInboxDismissals } from "@/components/teams/inbox/hooks/useInboxDismissals";
import { AttentionQueueRow } from "@/components/teams/inbox/AttentionQueueRow";
import { AgingItemRow, Curtain, ShelfHeader } from "@/components/teams/inbox/DecisionShelf";
import { handleShellLinkClick, shellUrlFromPaperclipPath, teamsDetailUrl } from "@/lib/shellNav";
import {
  ATTENTION_AGING_DAYS,
  attentionIsAging,
  buildDeskShelves,
  isInlineResolvable,
} from "@/components/teams/inbox/lib/attention";
import type { AttentionFeedResponse, AttentionItem, AttentionSubject, CompanyAgent } from "@/components/teams/shared/types";

const noopToggleExpand = () => {};

const EMPTY: ReadonlySet<string> = new Set();
type Pending = { stamp: unknown; hide: ReadonlySet<string>; restore: ReadonlySet<string> };

/** A failed write re-reads the feed (the hook's finally), which drops the
 *  optimistic state and puts the row back where it was. */
function settleQuietly(write: Promise<unknown>) {
  write.catch(() => {});
}

export interface AttentionDeskProps {
  agents?: readonly CompanyAgent[];
  currentUserId?: string | null;
  /** Leave the feed's `failed_run` rows out: For you lists the Inbox's failed-run
   *  groups (with Retry) under Needs you already, so the desk would repeat them. */
  hideFailedRuns?: boolean;
}

export function AttentionDesk({ agents = [], currentUserId = null, hideFailedRuns = false }: AttentionDeskProps) {
  const { read } = useTeamsApi();
  const { data: feed, isLoading, error } = read<AttentionFeedResponse>(teamsQueryKeys.attentionDesk());
  const { dismiss, snooze, restore } = useInboxDismissals();

  // `undefined` until the desk has auto-expanded its topmost inline decision (once).
  const [expandedId, setExpandedId] = useState<string | null | undefined>(undefined);
  const [collapsedShelves, setCollapsedShelves] = useState<Set<string>>(() => new Set());
  const [snoozedOpen, setSnoozedOpen] = useState(false);
  const [dismissedOpen, setDismissedOpen] = useState(false);
  const [agingOpen, setAgingOpen] = useState(false);

  const agentMap = useMemo(() => new Map(agents.map((agent) => [agent.id, agent])), [agents]);

  // Optimistic hide/restore, reset whenever a fresh feed lands (server truth):
  // held against the feed it was made on, the server's generatedAt (each read
  // is a fresh snapshot), or the feed object when an older feed carries none.
  const feedStamp: unknown = feed?.generatedAt ?? feed;
  const [pending, setPending] = useState<Pending>(() => ({ stamp: feedStamp, hide: EMPTY, restore: EMPTY }));
  const current = pending.stamp === feedStamp ? pending : { stamp: feedStamp, hide: EMPTY, restore: EMPTY };
  const pendingHide = current.hide;
  const pendingRestore = current.restore;
  // The updater reads the CURRENT feed's stamp, not the one its caller closed
  // over: a toast's Undo pressed after a newer feed landed must not wipe the
  // pending state made on that newer feed.
  const stampRef = useRef<unknown>(feedStamp);
  useEffect(() => {
    stampRef.current = feedStamp;
  }, [feedStamp]);
  const editPending = useCallback((field: "hide" | "restore", id: string, add: boolean) => {
    setPending((prev) => {
      const stamp = stampRef.current;
      const base = prev.stamp === stamp ? prev : { stamp, hide: EMPTY, restore: EMPTY };
      const next = new Set(base[field]);
      if (add) next.add(id);
      else next.delete(id);
      return { ...base, [field]: next };
    });
  }, []);

  const allItems = useMemo(() => {
    const items = feed?.items;
    if (!Array.isArray(items)) return [];
    return hideFailedRuns ? items.filter((item) => item.sourceKind !== "failed_run") : items;
  }, [feed, hideFailedRuns]);
  const activeItems = allItems.filter(
    (item) => (!item.dismissal?.isActive || pendingRestore.has(item.id)) && !pendingHide.has(item.id),
  );
  // The server's clock at feed time, so the shelves match the server's badge.
  // Date.now() only as a pre-load fallback, fixed at mount.
  const [mountedAt] = useState(() => Date.now());
  const now = feed?.generatedAt ? new Date(feed.generatedAt).getTime() : mountedAt;
  const agingItems = activeItems.filter(attentionIsAging);
  const deskItems = activeItems.filter((item) => !attentionIsAging(item));
  const hiddenOf = (kind: "snooze" | "dismiss") =>
    allItems.filter((item) => item.dismissal?.kind === kind && item.dismissal.isActive && !pendingRestore.has(item.id));
  const snoozedItems = hiddenOf("snooze");
  const dismissedItems = hiddenOf("dismiss");
  const shelves = buildDeskShelves(deskItems, now);

  // Auto-expand the topmost inline-capable decision, once (a render-time update,
  // so the first painted desk already shows it open).
  if (expandedId === undefined && deskItems.length > 0) {
    setExpandedId(shelves.flatMap((shelf) => shelf.items).find(isInlineResolvable)?.id ?? null);
  }

  const handleUndoDismiss = useCallback((item: AttentionItem) => {
    editPending("hide", item.id, false);
    if (item.dismissalKey) settleQuietly(restore(item.dismissalKey));
  }, [editPending, restore]);

  const handleDismiss = useCallback((item: AttentionItem) => {
    if (!item.dismissalKey) return;
    editPending("hide", item.id, true);
    settleQuietly(dismiss(item.dismissalKey));
    setExpandedId((previous) => (previous === item.id ? null : previous));
    notifySuccess("Dismissed", {
      dedupeKey: `attention-dismiss-${item.dismissalKey}`,
      description: item.subject.title ?? undefined,
      durationMs: 8000,
      action: { label: "Undo", onClick: () => handleUndoDismiss(item) },
    });
  }, [editPending, dismiss, handleUndoDismiss]);

  const handleSnooze = useCallback((item: AttentionItem, snoozedUntil: string) => {
    if (!item.dismissalKey) return;
    editPending("hide", item.id, true);
    settleQuietly(snooze(item.dismissalKey, snoozedUntil));
    setExpandedId((previous) => (previous === item.id ? null : previous));
  }, [editPending, snooze]);

  const handleRestore = useCallback((item: AttentionItem) => {
    if (!item.dismissalKey) return;
    editPending("restore", item.id, true);
    settleQuietly(restore(item.dismissalKey));
  }, [editPending, restore]);

  const handleToggleExpand = useCallback((item: AttentionItem) => {
    setExpandedId((prev) => (prev === item.id ? null : item.id));
  }, []);

  const toggleShelf = (key: string) =>
    setCollapsedShelves((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  if (isLoading && !feed) return <p role="status" className="for-you__empty">Loading…</p>;

  const hasAnything = activeItems.length > 0 || snoozedItems.length > 0 || dismissedItems.length > 0;
  const visibleCount = shelves.reduce((sum, shelf) => sum + shelf.items.length, 0);
  const hiddenRow = (item: AttentionItem) => (
    <AttentionQueueRow
      key={item.id}
      item={item}
      variant="hidden"
      expanded={false}
      onToggleExpand={noopToggleExpand}
      onDismiss={handleDismiss}
      onRestore={handleRestore}
      agentMap={agentMap}
      currentUserId={currentUserId}
    />
  );

  return (
    <div className="flex flex-col gap-4" data-testid="attention-desk">
      {error && (
        <p role="alert" className="text-caption text-[var(--err)]">
          {error instanceof Error && error.message ? error.message : "Couldn't load what needs you."}
        </p>
      )}

      {!hasAnything ? (
        !error && <ZeroState />
      ) : (
        <div className="flex flex-col gap-4">
          {visibleCount === 0 ? (
            <CaughtUpNote />
          ) : (
            shelves.map((shelf) => {
              const collapsed = collapsedShelves.has(shelf.key);
              return (
                <section key={shelf.key} className="flex flex-col gap-2" aria-label={shelf.label}>
                  <ShelfHeader
                    label={shelf.label}
                    collapsed={collapsed}
                    onToggle={() => toggleShelf(shelf.key)}
                    trailing={shelf.items.length}
                  />
                  {!collapsed && (
                    <div className="flex flex-col gap-4">
                      <BundledRows rows={shelf.items} agentMap={agentMap}>
                        {(item) => (
                          <AttentionQueueRow
                            item={item}
                            expanded={expandedId === item.id}
                            onToggleExpand={handleToggleExpand}
                            onDismiss={handleDismiss}
                            onSnooze={handleSnooze}
                            agentMap={agentMap}
                            currentUserId={currentUserId}
                          />
                        )}
                      </BundledRows>
                    </div>
                  )}
                </section>
              );
            })
          )}

          {snoozedItems.length > 0 && (
            <Curtain label="Snoozed" count={snoozedItems.length} open={snoozedOpen} onToggle={() => setSnoozedOpen((prev) => !prev)}>
              {snoozedItems.map(hiddenRow)}
            </Curtain>
          )}

          {dismissedItems.length > 0 && (
            <Curtain label="Dismissed" count={dismissedItems.length} open={dismissedOpen} onToggle={() => setDismissedOpen((prev) => !prev)}>
              {dismissedItems.map(hiddenRow)}
            </Curtain>
          )}

          {agingItems.length > 0 && (
            <Curtain label="Aging" count={agingItems.length} open={agingOpen} onToggle={() => setAgingOpen((prev) => !prev)}>
              <p className="text-caption text-[var(--fg-mute)]">
                Idle past {ATTENTION_AGING_DAYS} days — kept off the desk.
              </p>
              {agingItems.map((item) => (
                <AgingItemRow
                  key={item.id}
                  item={item}
                  now={now}
                  agentMap={agentMap}
                  currentUserId={currentUserId}
                  expanded={expandedId === item.id}
                  onToggleExpand={handleToggleExpand}
                  onDismiss={handleDismiss}
                  onSnooze={handleSnooze}
                />
              ))}
            </Curtain>
          )}
        </div>
      )}
    </div>
  );
}

const bundleIdOf = (item: AttentionItem) =>
  item.sourceKind === "decision" ? ((item.subject.metadata?.bundleId as string | null | undefined) ?? null) : null;

/** A shelf's rows, with one bundle header over each run of decisions that share a bundle. */
function BundledRows({
  rows,
  agentMap,
  children,
}: {
  rows: AttentionItem[];
  agentMap: Map<string, CompanyAgent>;
  children: (item: AttentionItem) => ReactNode;
}) {
  const seen = new Set<string>();
  return (
    <>
      {rows.map((item) => {
        const bundleId = bundleIdOf(item);
        let header: ReactNode = null;
        if (bundleId && !seen.has(bundleId)) {
          seen.add(bundleId);
          const bundleRows = rows.filter((row) => bundleIdOf(row) === bundleId);
          const first = bundleRows[0];
          header = (
            <DecisionBundleHeader
              agentName={agentMap.get(first?.subject.metadata?.originAgentId as string)?.name ?? null}
              title={(first?.subject.metadata?.bundleTitle as string | null | undefined) ?? null}
              originIssue={first?.relatedIssue ?? null}
              count={bundleRows.length}
            />
          );
        }
        return (
          <Fragment key={item.id}>
            {header}
            <div className={bundleId ? "border-l-2 border-[var(--edge)] pl-3" : undefined}>{children(item)}</div>
          </Fragment>
        );
      })}
    </>
  );
}

/** "Planner proposed 6 decisions · from PAP-123 · routing review · 6 pending". */
export function DecisionBundleHeader({
  agentName,
  title,
  originIssue,
  count,
}: {
  agentName: string | null;
  title: string | null;
  originIssue: AttentionSubject | null;
  count: number;
}) {
  const noun = count === 1 ? "decision" : "decisions";
  const originUrl = originIssue
    ? shellUrlFromPaperclipPath(originIssue.href) ?? teamsDetailUrl("issue", originIssue.id)
    : null;
  const originLabel = originIssue?.identifier ?? originIssue?.title;
  return (
    <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5 rounded-sm border-l-2 border-[var(--edge)] px-3 py-1.5 text-caption">
      <span className="font-medium text-[var(--fg)]">
        {agentName ?? "An agent"} proposed {count} {noun}
      </span>
      {originLabel && (
        <span className="text-[var(--fg-mute)]">
          {"· from "}
          {originUrl ? (
            <a href={originUrl} className="hover:underline" onClick={(e) => handleShellLinkClick(e, originUrl)}>
              {originLabel}
            </a>
          ) : (
            originLabel
          )}
        </span>
      )}
      {title && <span className="text-[var(--fg-mute)]">· {title}</span>}
      <span className="text-[var(--fg-mute)]">· {count} pending</span>
    </div>
  );
}

function CaughtUpNote() {
  return (
    <div className="rounded-xl border border-dashed border-[var(--edge)] py-10 text-center">
      <p className="text-ui font-medium text-[var(--fg)]">You&apos;re all caught up.</p>
    </div>
  );
}

function ZeroState() {
  return (
    <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-[var(--edge)] py-20 text-center">
      <CheckCircle2 size={40} className="mb-4 text-[var(--ok)]" aria-hidden="true" />
      <p className="text-ui font-semibold text-[var(--fg)]">You&apos;re all caught up</p>
      <p className="mt-1 flex items-center gap-1.5 text-caption text-[var(--fg-mute)]">
        <Inbox size={16} aria-hidden="true" />
        Nothing needs a decision from you right now.
      </p>
    </div>
  );
}
