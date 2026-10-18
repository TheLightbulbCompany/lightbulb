// apps/frontend/src/components/teams/inbox/hooks/useAttentionFeed.ts
"use client";

import { useMemo } from "react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { teamsQueryKeys } from "@/components/teams/shared/queryKeys";
import type {
  AttentionFeedResponse,
  AttentionItem,
  AttentionSourceKind,
} from "@/components/teams/shared/types";

/** The full SWR cache key, for mutate() after resolving an item. */
export const ATTENTION_KEY = `/teams${teamsQueryKeys.attention()}`;

/** Every attention read: this badge key and any query-stringed variant (the
 *  Activity desk's `?includeDismissed=true&all=true`). Pass to mutate() after a
 *  change that moves an item, so the badge and the desk refresh together. */
export function isAttentionKey(key: string): boolean {
  return key === ATTENTION_KEY || key.startsWith(`${ATTENTION_KEY}?`);
}

/** The source kinds where work does not move until a HUMAN does something.
 *
 *  That is the line this list draws, and it is drawn from the owner's own
 *  description of the problem: approvals they could not find, and "blocked
 *  tasks" it was unclear they were expected to clear. Both are here.
 *
 *  Excluded are the feed's health and advisory rows — `failed_run`,
 *  `agent_error_alert`, `budget_alert`, `productivity_review`. Those report
 *  what happened rather than ask for anything, so counting them would inflate
 *  a badge that is supposed to mean "this many things are stuck on you".
 *  Failed runs are still excluded here for that reason, but they no longer
 *  have a separate home: the Agent failures band was deleted with the Inbox
 *  pill tabs, and `InboxNeedsYou` now renders this feed and the failed-run
 *  feed (`useInboxTriage`) together under one "Needs you" label.
 */
const DECISION_KINDS: ReadonlySet<AttentionSourceKind> = new Set([
  "approval",
  "issue_thread_interaction",
  "decision",
  "join_request",
  "review",
  // A task blocked behind something a human has to clear — named explicitly
  // in the complaint that motivated this surface.
  "blocker_attention",
  // An agent asking to be recovered; nothing proceeds until someone acts.
  "recovery_action",
]);

// NOT here: a fired-but-unhandled issue monitor (#2007's known gap). It IS a
// "needs a human" state, but upstream's attention service emits no monitor
// source kind — `monitorLastTriggeredAt` appears nowhere in it, and the
// `review` rule treats an existing monitor as a reason NOT to raise a row.
// The only way to surface one today is to scan issue rows for
// `executionState.monitor`, which is the client-side scan this surface exists
// to avoid (the issue list silently caps ~120 rows, isol8 #1992) and would put
// a second, disagreeing source behind a badge whose whole promise is one
// number. The correct fix is upstream (or the Isol8 Paperclip fork) emitting a
// `monitor` source kind; once it does, this becomes one entry here plus a
// label in AttentionList's SOURCE_LABEL, and it inherits everything else.

export interface UseAttentionFeedResult {
  /** Decision items, in the server's rank order. */
  items: AttentionItem[];
  /**
   * How many decisions are waiting company-wide.
   *
   * Summed from `countsBySourceKind` over the decision kinds, NOT from
   * `items.length`. Upstream builds those counts from the FULL ranked set and
   * only then slices the page (`rankedItems` vs `items` in
   * services/attention.ts), so this stays true past the 50-row page default —
   * a 51st pending decision still shows up in the badge.
   *
   * Not the feed's `totalCount` either: that includes the health rows filtered
   * out above, so it would badge a number this list cannot account for.
   *
   * Falls back to `items.length` only when the counts are missing entirely
   * (malformed upstream body). That under-reports rather than inventing a
   * number, which is the correct direction to be wrong in.
   */
  count: number;
  isLoading: boolean;
  isError: boolean;
  /** Re-read the feed — call after resolving an item so it leaves the list. */
  refresh: () => void;
}

/**
 * The single source for "what needs this human".
 *
 * One request, server-ranked, covering both entity types the product asks a
 * human to answer: board approvals and issue-thread interaction cards. Every
 * consumer (the tab list, the tab badge, the inbox pointer) reads THIS hook,
 * so no two surfaces can show contradicting counts.
 *
 * Deliberately not built by scanning issues client-side: the Teams issue list
 * silently caps around 120 rows (isol8 #1992), so a client-side derivation
 * would quietly under-report the moment a company got busy.
 */
export function useAttentionFeed(): UseAttentionFeedResult {
  const { read, mutate } = useTeamsApi();
  const { data, isLoading, error } = read<AttentionFeedResponse>(
    teamsQueryKeys.attention(),
  );

  const items = useMemo(() => {
    const rows = data?.items;
    if (!Array.isArray(rows)) return [];
    return rows.filter((item) => DECISION_KINDS.has(item.sourceKind));
  }, [data]);

  // Sum the decision kinds out of the pre-pagination source counts. `null`
  // means upstream sent none at all, which is the only case that falls back
  // to the page length.
  const sourceCount = useMemo(() => {
    const counts = data?.countsBySourceKind;
    if (!counts || typeof counts !== "object") return null;
    let total = 0;
    let sawAny = false;
    for (const kind of DECISION_KINDS) {
      const n = counts[kind];
      if (typeof n === "number" && Number.isFinite(n)) {
        total += n;
        sawAny = true;
      }
    }
    return sawAny ? total : null;
  }, [data]);

  return {
    items,
    count: sourceCount ?? items.length,
    isLoading,
    isError: Boolean(error),
    // Every attention key, so Retry and a resolved item also refresh the desk.
    refresh: () => {
      void mutate(isAttentionKey);
    },
  };
}
