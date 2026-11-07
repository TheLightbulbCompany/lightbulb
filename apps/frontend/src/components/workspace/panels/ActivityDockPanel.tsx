"use client";

import "@/components/activity/activity.css";
import { useEffect } from "react";
import { useAuth } from "@clerk/nextjs";
import { newestActivityAt, type CanonicalRoomMessage } from "@lightbulb/shared";
import { ForYouAsks, rowTarget, type ActivityTarget } from "@/components/activity/ForYou";
import { homeExtras } from "@/components/activity/activityPage";
import { HomeIdeas } from "@/components/ideas/HomeIdeas";
import { FIRST_MOVE_LABEL, openFirstMoves, useFirstMoves } from "@/components/ideas/FirstMoves";
import { TeamFeed } from "@/components/activity/TeamFeed";
import { draftKey, readDraft, writeDraft } from "@/components/messaging/drafts";
import { useActivity } from "@/hooks/useActivity";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import type { ActivityItem } from "@lightbulb/shared";
import { HomeWork } from "@/components/tasks/HomeWork";
import { pageVisible } from "@/hooks/useRooms";
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

/** Activity: "your company today" (TodayHome). */
export function ActivityDockPanel(props: PanelProps) {
  return <TodayHome scheduledOpen={props.params.section === "scheduled"} />;
}

/** Feed posts Home shows before "See the whole feed". */
export const HOME_FEED_POSTS = 5;

/** Your company today, top to bottom: Needs you (decision cards and non-task asks); your mentions (one line); Ideas
 *  (Do it / Not now); the feed's newest posts under its editable Brief; then Working, Done and Closed. */
function TodayHome({ scheduledOpen }: { scheduledOpen: boolean }) {
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
  // First run: one row while its ideas wait in the Lightbulb DM (it is in the one count too).
  const firstMoves = useFirstMoves();
  // Frame A: the 880 column, titled.
  return <div className="activity-page activity-page--home activity-page--today"><div className="frame-doc">
    <div className="frame-doc__title"><h1>Activity</h1></div>
    <HomeWork viewerId={userId ?? ""} onOpen={openTaskPane} askCount={asks.length + (firstMoves.length ? 1 : 0)} scheduledOpen={scheduledOpen}
      asks={<>
        {firstMoves.length > 0 && <button type="button" className="mt-1 flex min-h-[42px] w-full items-center gap-2 rounded-md px-2 text-left hover:bg-accent" onClick={openFirstMoves}>
          <span aria-hidden="true" className="size-2 flex-none rounded-full bg-warn-hue" />
          <span className="min-w-0 flex-1 truncate"><span className="font-medium">{FIRST_MOVE_LABEL}</span>
            <span className="text-muted-foreground"> — {firstMoves.length} {firstMoves.length === 1 ? "idea" : "ideas"} from Lightbulb</span></span>
        </button>}
        <ForYouAsks items={asks} onRunAction={activity.runAction} onNavigate={openActivityTarget} />
      </>} between={<>
      <HomeForYou items={activity.items} />
      <HomeIdeas />
      <TeamFeed client={live.messaging} scope={scope} currentUserId={userId ?? ""} limit={HOME_FEED_POSTS}
        onDiscuss={(post) => openDiscussion(scope, post)} isVisible={pageVisible} />
    </>} />
  </div></div>;
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
