"use client";

import { useMemo } from "react";
import { useAuth } from "@clerk/nextjs";
import type { ActivityItem } from "@lightbulb/shared";
import { useActivity } from "@/hooks/useActivity";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { teamsQueryKeys } from "@/components/teams/shared/queryKeys";
import { useAcknowledgedRuns } from "@/components/teams/inbox/hooks/useAcknowledgedRuns";
import { useInboxTriage } from "@/components/teams/inbox/hooks/useInboxTriage";
import { groupFailedRuns } from "@/components/teams/inbox/lib/groupFailedRuns";
import type { AttentionFeedResponse } from "@/components/teams/shared/types";

/** Activity's Paperclip asks that For you → Needs you shows on the desk or as a failed-run group instead. */
function coveredByNeedsYou(item: ActivityItem, runsKnown: boolean): boolean {
  if (item.kind !== "paperclip" || item.state !== "open") return false;
  if (item.subtype === "approval" || item.subtype === "interaction") return true;
  return runsKnown && item.subtype === "failed_run";
}

/**
 * What the web rail's Activity badge counts, so it agrees with For you → Needs you.
 *
 * The server's `attentionCount` counts every needs-you Activity item plus unseen
 * messages; its Paperclip approvals, questions and failed runs are swapped for
 * what Needs you actually shows: the attention desk's items (the feed upstream
 * already returns without dismissed and snoozed rows, its desk's failed runs
 * left out) and the Inbox's failed-run groups. A read that is missing or failed
 * leaves the server's count for that part, as other clients show it.
 */
export function needsYouBadge(input: {
  serverCount: number;
  activityItems: readonly ActivityItem[];
  attention: AttentionFeedResponse | null;
  failedRunGroups: number | null;
}): number {
  const { serverCount, activityItems, attention, failedRunGroups } = input;
  if (!attention) return serverCount;
  const runsKnown = failedRunGroups !== null;
  const covered = activityItems.filter((item) => coveredByNeedsYou(item, runsKnown)).length;
  return Math.max(0, serverCount - covered) + deskCount(attention) + (failedRunGroups ?? 0);
}

/** Desk items the badge counts: every shown kind but `failed_run` (the Inbox groups carry those). */
function deskCount(feed: AttentionFeedResponse): number {
  const counts = feed.countsBySourceKind;
  if (counts && typeof counts === "object") {
    let total = 0;
    for (const [kind, n] of Object.entries(counts)) {
      if (kind !== "failed_run" && typeof n === "number" && Number.isFinite(n)) total += n;
    }
    return total;
  }
  // No counts (malformed body): the page, minus anything hidden.
  return (feed.items ?? []).filter((item) => !item.dismissal?.isActive && item.sourceKind !== "failed_run").length;
}

/** The rail's Activity badge, from reads the Activity page makes anyway (SWR shares them). */
export function useNeedsYouBadge(): number {
  const { userId, orgId } = useAuth();
  const activity = useActivity();
  const { read } = useTeamsApi();
  // `/attention`, the badge read: upstream leaves dismissed and snoozed items out of it.
  const { data: attention, error: attentionError } = read<AttentionFeedResponse>(teamsQueryKeys.attention());
  const { acknowledgedRunIds } = useAcknowledgedRuns(orgId ?? userId ?? null);
  const runs = useInboxTriage(acknowledgedRunIds);
  const groups = useMemo(() => groupFailedRuns(runs.failedRuns).length, [runs.failedRuns]);
  return needsYouBadge({
    serverCount: activity.attentionCount,
    activityItems: activity.items,
    attention: attention && !attentionError ? attention : null,
    failedRunGroups: runs.isError || runs.isLoading ? null : groups,
  });
}
