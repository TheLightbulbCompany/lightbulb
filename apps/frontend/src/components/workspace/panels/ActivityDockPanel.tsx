"use client";

import "@/components/activity/activity.css";
import { useEffect, useMemo, useRef, useState } from "react";
import { useAuth } from "@clerk/nextjs";
import { newestActivityAt, type CanonicalRoomMessage } from "@lightbulb/shared";
import { ForYou, ForYouAsks, rowTarget, type ActivityTarget } from "@/components/activity/ForYou";
import { homeExtras, isForYouFilter } from "@/components/activity/activityPage";
import { HomeIdeas } from "@/components/ideas/HomeIdeas";
import { TeamFeed } from "@/components/activity/TeamFeed";
import { draftKey, readDraft, writeDraft } from "@/components/messaging/drafts";
import { useAcknowledgedRuns } from "@/components/teams/inbox/hooks/useAcknowledgedRuns";
import { useAttentionFeed } from "@/components/teams/inbox/hooks/useAttentionFeed";
import { useInboxTriage } from "@/components/teams/inbox/hooks/useInboxTriage";
import { groupFailedRuns } from "@/components/teams/inbox/lib/groupFailedRuns";
import type { CompanyAgent, CompanyMember } from "@/components/teams/shared/types";
import { useActivity } from "@/hooks/useActivity";
import { useLiveEvents, useSuperAgent } from "@/hooks/useLiveEvents";
import type { ActivityItem } from "@lightbulb/shared";
import { HomeWork } from "@/components/tasks/HomeWork";
import { pageVisible } from "@/hooks/useRooms";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { openPanel, openTaskPane } from "../workspaceController";
import type { PanelProps } from "../panelComponents";

const TEAMS_DETAIL: Partial<Record<ActivityTarget["kind"], string>> = {
  issue: "teams-issue-detail",
  run: "teams-run-detail",
  approval: "teams-approval-detail",
};

/**
 * Opens an item in context: a room or thread in Messages, anything Paperclip in its Teams detail.
 * Rooms and threads go through the `openRoom` event a notification click uses: the Messages
 * provider selects the room, opens the thread on its root and brings the panel forward.
 * (`openPanel("chat", …)` only retargets an agent DM, so a room link through it did nothing.)
 */
export function openActivityTarget({ kind, id, roomId }: ActivityTarget): void {
  // A super-agent thread (a task question's "Open thread"): the thread pane, not a room.
  if (kind === "task") {
    openTaskPane(id);
    return;
  }
  if (kind === "session") {
    window.dispatchEvent(new CustomEvent("openRoom", { detail: { sessionId: id } }));
    return;
  }
  if (kind === "room" || kind === "thread") {
    const room = kind === "room" ? id : roomId;
    if (room) window.dispatchEvent(new CustomEvent("openRoom", { detail: { roomId: room, threadId: kind === "thread" ? id : null } }));
    return;
  }
  const panel = TEAMS_DETAIL[kind];
  if (panel) openPanel(panel, { detailId: id }, { pushUrl: true });
}

/**
 * Discuss: the post's thread in the Messages thread pane, its author @mentioned in the composer
 * (the mention is what wakes an agent there). A draft the person already started is kept.
 * `scope` is the Messages draft scope (org, else user).
 */
export function openDiscussion(scope: string, post: CanonicalRoomMessage): void {
  const key = draftKey(scope, post.roomId, post.messageId);
  if (!readDraft(key).text.trim() && post.author.kind !== "system") {
    writeDraft(key, { text: `@${post.author.displayName} `, mentions: [{ kind: post.author.kind, id: post.author.id }] });
  }
  window.dispatchEvent(new CustomEvent("openRoom", { detail: { roomId: post.roomId, threadId: post.messageId } }));
}

type ActivityTab = "for-you" | "feed";

/** useInboxTriage's SWR key (useTeamsApi prefixes /teams), for Retry. */
const INBOX_RUNS_KEY = "/teams/inbox/runs";

/** Whether the feed column is on screen: both columns when wide, the Feed tab when narrow, never before the layout is measured. */
export function feedShown(narrow: boolean | null, tab: ActivityTab): boolean {
  return narrow === false || (narrow === true && tab === "feed");
}

/** Home. With the super-agent on, "your company today" (TodayHome); otherwise Activity's For you beside the team feed
 *  (tabs below ~900px). */
export function ActivityDockPanel(props: PanelProps) {
  return useSuperAgent() ? <TodayHome /> : <ActivityHome {...props} />;
}

/** Feed posts Home shows before "See the whole feed". */
export const HOME_FEED_POSTS = 5;

/** Your company today, top to bottom: Needs you (decision cards and non-task asks); your mentions (one line); Ideas
 *  (Do it / Not now); the feed's newest posts under its editable Brief; then Working, Done and Closed. */
function TodayHome() {
  const { userId, orgId } = useAuth();
  const live = useLiveEvents();
  const activity = useActivity();
  const scope = orgId ?? userId ?? "signed-out";
  // Opening Home clears the Activity badge; a newer item arriving while it is on screen does too.
  const newest = newestActivityAt(activity.items);
  const { markSeen } = activity;
  useEffect(() => {
    if (newest && pageVisible()) void markSeen(newest).catch(() => {});
  }, [newest, markSeen]);
  const { asks } = homeExtras(activity.items);
  return <div className="activity-page activity-page--home activity-page--today">
    <HomeWork viewerId={userId ?? ""} onOpen={openTaskPane} askCount={asks.length}
      asks={<ForYouAsks items={asks} onRunAction={activity.runAction} onNavigate={openActivityTarget} />} between={<>
      <HomeForYou items={activity.items} />
      <HomeIdeas />
      <TeamFeed client={live.messaging} scope={scope} currentUserId={userId ?? ""} limit={HOME_FEED_POSTS}
        onDiscuss={(post) => openDiscussion(scope, post)} isVisible={pageVisible} />
    </>} />
  </div>;
}

/** Mentions as one line (how many, who, the newest). Open asks that are not tasks sit in Needs you, in its count. */
export function HomeForYou({ items }: { items: readonly ActivityItem[] }) {
  const { mentions } = homeExtras(items);
  if (!mentions.length) return null;
  const newest = mentions[0];
  const target = rowTarget(newest);
  return <section aria-label="Mentions" className="home-mentions">
    <button type="button" className="truncate hover:text-foreground" disabled={!target} onClick={() => target && openActivityTarget(target)}>
      <span className="font-semibold text-foreground">Mentions · {mentions.length}</span> · {newest.actor.name}: {newest.summary.preview?.text || newest.summary.title}
    </button>
  </section>;
}

function ActivityHome({ params }: PanelProps) {
  const { userId, orgId } = useAuth();
  const live = useLiveEvents();
  const activity = useActivity();
  const attention = useAttentionFeed();
  const teams = useTeamsApi();
  const { data: humans } = teams.read<{ members?: CompanyMember[] }>("/members");
  const { data: agents } = teams.read<{ agents?: CompanyAgent[] }>("/agents");
  const people = useMemo(() => [
    ...(humans?.members ?? []).map((member) => ({ value: member.userId, label: member.name || member.email || "Workspace member" })),
    ...(agents?.agents ?? []).map((agent) => ({ value: agent.id, label: agent.name })),
  ], [humans, agents]);
  const roster = useMemo(() => agents?.agents ?? [], [agents]);
  const [tab, setTab] = useState<ActivityTab>("for-you");
  const scope = orgId ?? userId ?? "signed-out";
  // The Inbox's failed runs, with its local dismissals (same company key as the Inbox panel).
  const { acknowledgedRunIds, ack } = useAcknowledgedRuns(orgId ?? userId ?? null);
  const { failedRuns, isError: runsError, isLoading: runsLoading } = useInboxTriage(acknowledgedRunIds);
  const runGroups = useMemo(() => groupFailedRuns(failedRuns), [failedRuns]);
  // Below 900px (the CSS container query) only one column shows: the feed counts as read
  // only while its tab is the one on screen.
  const pageRef = useRef<HTMLDivElement>(null);
  // Unknown until the first measurement: a hidden feed must not count as read in between.
  const [narrow, setNarrow] = useState<boolean | null>(null);
  useEffect(() => {
    const page = pageRef.current;
    if (!page || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => setNarrow(entry.contentRect.width <= 900));
    observer.observe(page);
    return () => observer.disconnect();
  }, []);

  // Opening Activity clears its badge; a newer item arriving while it is on screen does too.
  const newest = newestActivityAt(activity.items);
  const { markSeen } = activity;
  useEffect(() => {
    if (newest && pageVisible()) void markSeen(newest).catch(() => {});
  }, [newest, markSeen]);

  return <div ref={pageRef} className="activity-page" data-tab={tab}>
    <div role="tablist" aria-label="Activity" className="activity-page__tabs">
      {([["for-you", "For you"], ["feed", "Feed"]] as const).map(([value, label]) =>
        <button key={value} type="button" role="tab" aria-selected={tab === value} onClick={() => setTab(value)}>{label}</button>)}
    </div>
    <div className="activity-page__columns">
      <div className="activity-page__for-you">
        <ForYou
          activityItems={activity.items}
          attentionItems={attention.items}
          failedRuns={runGroups}
          onDismissRuns={(ids) => ids.forEach(ack)}
          loading={activity.loading || attention.isLoading || runsLoading}
          incomplete={Boolean(activity.error) || !activity.paperclipAvailable || attention.isError || runsError}
          onRetry={() => { void activity.refresh(); attention.refresh(); void teams.mutate(INBOX_RUNS_KEY); }}
          // A failed page stops the scroll paging (else the sentinel refetches it in a loop); Retry restarts it.
          hasMore={activity.hasMore && !activity.error}
          loadingMore={activity.loadingMore}
          onLoadMore={activity.loadMore}
          onRunAction={activity.runAction}
          onNavigate={openActivityTarget}
          people={people}
          agents={roster}
          focusId={params.focus ?? null}
          currentUserId={userId ?? null}
          filter={isForYouFilter(params.filter) ? params.filter : "all"}
          onFilterChange={(filter) => openPanel("activity", filter === "all" ? {} : { filter }, { pushUrl: true })}
        />
      </div>
      <div className="activity-page__feed">
        <TeamFeed client={live.messaging} scope={scope} currentUserId={userId ?? ""}
          onDiscuss={(post) => openDiscussion(scope, post)} isVisible={pageVisible} active={feedShown(narrow, tab)} />
      </div>
    </div>
  </div>;
}
