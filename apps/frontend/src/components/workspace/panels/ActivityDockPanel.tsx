"use client";

import "@/components/activity/activity.css";
import { useEffect, useMemo, useRef, useState } from "react";
import type { IDockviewPanelProps } from "dockview-react";
import { useAuth } from "@clerk/nextjs";
import { newestActivityAt, type CanonicalRoomMessage } from "@lightbulb/shared";
import { ForYou, type ActivityTarget } from "@/components/activity/ForYou";
import { TeamFeed } from "@/components/activity/TeamFeed";
import { draftKey, readDraft, writeDraft } from "@/components/messaging/drafts";
import { useAcknowledgedRuns } from "@/components/teams/inbox/hooks/useAcknowledgedRuns";
import { useAttentionFeed } from "@/components/teams/inbox/hooks/useAttentionFeed";
import { useInboxTriage } from "@/components/teams/inbox/hooks/useInboxTriage";
import { groupFailedRuns } from "@/components/teams/inbox/lib/groupFailedRuns";
import type { CompanyAgent, CompanyMember } from "@/components/teams/shared/types";
import { useActivity } from "@/hooks/useActivity";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { pageVisible } from "@/hooks/useRooms";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { openPanel } from "../workspaceController";
import { useDockPanelParams } from "./useDockPanelParams";

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

/** Activity: For you on the left, the team feed on the right; tabs below ~900px. */
export function ActivityDockPanel(props: IDockviewPanelProps<{ focus?: string }>) {
  const params = useDockPanelParams(props);
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
  const { failedRuns, isError: runsError } = useInboxTriage(acknowledgedRunIds);
  const runGroups = useMemo(() => groupFailedRuns(failedRuns), [failedRuns]);
  // Below 900px (the CSS container query) only one column shows: the feed counts as read
  // only while its tab is the one on screen.
  const pageRef = useRef<HTMLDivElement>(null);
  const [narrow, setNarrow] = useState(false);
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
          runsError={runsError}
          onDismissRuns={(ids) => ids.forEach(ack)}
          loading={activity.loading}
          error={Boolean(activity.error)}
          paperclipAvailable={activity.paperclipAvailable}
          onRunAction={activity.runAction}
          onNavigate={openActivityTarget}
          people={people}
          agents={roster}
          focusId={params.focus ?? null}
        />
      </div>
      <div className="activity-page__feed">
        <TeamFeed client={live.messaging} scope={scope} currentUserId={userId ?? ""}
          onDiscuss={(post) => openDiscussion(scope, post)} isVisible={pageVisible} active={!narrow || tab === "feed"} />
      </div>
    </div>
  </div>;
}
