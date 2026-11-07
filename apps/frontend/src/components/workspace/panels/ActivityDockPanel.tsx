"use client";

import "@/components/activity/activity.css";
import { useEffect } from "react";
import { useAuth } from "@clerk/nextjs";
import { newestActivityAt } from "@lightbulb/shared";
import { ForYouAsks, type ActivityTarget } from "@/components/activity/ForYou";
import { homeExtras } from "@/components/activity/activityPage";
import { useActivity } from "@/hooks/useActivity";
import { HomeWork } from "@/components/tasks/HomeWork";
import { pageVisible } from "@/hooks/useRooms";
import { openPanel, openTaskPane } from "../workspaceController";
import type { PanelProps } from "../panelComponents";

/**
 * Opens an item in context: a room or thread in Messages, a task in its pane. An old Paperclip item
 * (issue, run, approval) has no page any more: it opens Activity, where it is listed.
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
  openPanel("activity", {}, { pushUrl: true });
}

/** Activity: "your company today" (TodayHome). */
export function ActivityDockPanel(props: PanelProps) {
  return <TodayHome scheduledOpen={props.params.section === "scheduled"} />;
}

/** Your company today, top to bottom: Needs you (decision cards and non-task asks), then Working, Done and Closed. */
function TodayHome({ scheduledOpen }: { scheduledOpen: boolean }) {
  const { userId } = useAuth();
  const activity = useActivity();
  // Opening Home clears the Activity badge; a newer item arriving while it is on screen does too.
  const newest = newestActivityAt(activity.items);
  const { markSeen } = activity;
  useEffect(() => {
    if (newest && pageVisible()) void markSeen(newest).catch(() => {});
  }, [newest, markSeen]);
  const { asks } = homeExtras(activity.items);
  // Frame A: the 880 column, titled.
  return <div className="activity-page activity-page--home activity-page--today"><div className="frame-doc">
    <div className="frame-doc__title"><h1>Activity</h1></div>
    <HomeWork viewerId={userId ?? ""} onOpen={openTaskPane} askCount={asks.length} scheduledOpen={scheduledOpen}
      asks={<ForYouAsks items={asks} onRunAction={activity.runAction} onNavigate={openActivityTarget} />} />
  </div></div>;
}
