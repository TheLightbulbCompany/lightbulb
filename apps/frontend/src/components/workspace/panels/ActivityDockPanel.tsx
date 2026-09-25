"use client";

import { useEffect, useMemo } from "react";
import type { IDockviewPanelProps } from "dockview-react";
import { isActivityFilter, newestActivityAt } from "@lightbulb/shared";
import { ActivityFeed, type ActivityTarget } from "@/components/activity/ActivityFeed";
import type { CompanyAgent, CompanyMember } from "@/components/teams/shared/types";
import { useActivity } from "@/hooks/useActivity";
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

export function ActivityDockPanel(props: IDockviewPanelProps<{ filter?: string; focus?: string }>) {
  const params = useDockPanelParams(props);
  const activity = useActivity();
  const teams = useTeamsApi();
  const { data: humans } = teams.read<{ members?: CompanyMember[] }>("/members");
  const { data: agents } = teams.read<{ agents?: CompanyAgent[] }>("/agents");
  const people = useMemo(() => [
    ...(humans?.members ?? []).map((member) => ({ value: member.userId, label: member.name || member.email || "Workspace member" })),
    ...(agents?.agents ?? []).map((agent) => ({ value: agent.id, label: agent.name })),
  ], [humans, agents]);

  // Opening the feed clears its badge; a newer item arriving while it is on screen does too.
  const newest = newestActivityAt(activity.items);
  const { markSeen } = activity;
  useEffect(() => {
    if (newest && pageVisible()) void markSeen(newest).catch(() => {});
  }, [newest, markSeen]);

  return <ActivityFeed
    items={activity.items}
    loading={activity.loading || activity.loadingMore}
    error={Boolean(activity.error)}
    paperclipAvailable={activity.paperclipAvailable}
    hasMore={activity.hasMore}
    onLoadMore={activity.loadMore}
    onRunAction={activity.runAction}
    onNavigate={openActivityTarget}
    people={people}
    onOpenCompanyActivity={() => openPanel("teams-inbox", {}, { pushUrl: true })}
    initialFilter={isActivityFilter(params.filter) ? params.filter : undefined}
    focusId={params.focus ?? null}
  />;
}
