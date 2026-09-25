"use client";

import { useEffect, useRef, useSyncExternalStore } from "react";
import { LoaderCircle } from "lucide-react";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import {
  approvalActivityId,
  approvalNotificationCopy,
  interactionActivityId,
  notificationLine,
  notificationTitle,
  roomWhere,
  runFailureLine,
  type ActivityItem,
} from "@lightbulb/shared";
import { useApi } from "@/lib/api";
import { useAgents, agentDisplayName, type Agent } from "@/hooks/useAgents";
import { useActivity } from "@/hooks/useActivity";
import { openPanel, useWorkspaceState } from "@/components/workspace/workspaceController";
import { capture } from "@/lib/analytics";
import {
  notifySuccess,
  notifyFailure,
  notifyAttention,
  dismissNotification,
} from "@/lib/notify";
import { nativeNotify } from "@/lib/desktopNotify";
import { pushSearchParams } from "@/lib/shellNav";
import {
  createRunToastState,
  reduceChatFrame,
  reduceTeamsRunEvent,
  reduceTeamsActivityEvent,
  type RunToastAction,
  type RunToastContext,
} from "./runToastReducer";
import { trackTeamRunActivity, useActiveTeamRunCount } from "./teamRunActivity";
import { getRoomView, subscribeRoomView } from "./roomView";
import "./run-notifications.css";

/**
 * Global bottom-right run notifications. Renders nothing; consumes the
 * active-room chat-frame stream plus paperclip events, and drives one live
 * toast per run keyed by runId.
 *
 * Every handler is wrapped so a notification bug can never break chat.
 */
// Mobile (single-pane page.tsx) threads the selected agent + current view/panel
// and derives a per-session watched key. Desktop (the dockview shell) derives
// the watched key set from the live workspace snapshot — every visible chat
// panel — so it needs no props. The two are a discriminated union so each mode
// only carries the fields it can actually supply.
export type RunNotificationsProps =
  | {
      mode: "mobile";
      selectedAgentId: string | null;
      activeView: "chat" | "control";
      activePanel: string;
    }
  | { mode: "desktop" };

function agentName(agentId: string, agents: Agent[]): string {
  const agent = agents.find((a) => a.id === agentId);
  return agent ? agentDisplayName(agent) : "Agent";
}

function buildDesktopWatchedKeys(visibleChatAgentIds: string[]): Set<string> {
  return new Set(visibleChatAgentIds);
}

function viewAgentAction(agentId: string, roomId?: string): { label: string; onClick: () => void } {
  return {
    label: "View",
    onClick: () => {
      capture("run_notification_clicked", { source: "chat" });
      // The run's own room (a channel run must not land on an empty DM);
      // MessagingWorkspaceContext opens it exactly like a message notification.
      if (roomId) {
        window.dispatchEvent(new CustomEvent("openRoom", { detail: { roomId, threadId: null } }));
        return;
      }
      // ChatLayout owns selection (sidebar, session store, ?panel cleanup); it
      // listens for externally-sourced selectAgent events and routes them
      // through its own handleSelectAgent.
      window.dispatchEvent(
        new CustomEvent("selectAgent", {
          detail: { agentId, source: "notification" },
        }),
      );
    },
  };
}

// Both toast families land on the ONE Inbox page, at the band that matches the
// toast: attention toasts (approvals, blocked) are decisions → Needs you;
// background-run toasts (working, success, failure) are history → Recently.
function viewTeamsAction(section: "needs-you" | "recently"): {
  label: string;
  onClick: () => void;
} {
  return {
    label: "View",
    onClick: () => {
      capture("run_notification_clicked", {
        source: section === "needs-you" ? "approval" : "teams",
      });
      const params = new URLSearchParams(window.location.search);
      params.set("panel", "teams-inbox");
      params.set("section", section);
      params.delete("detailId");
      pushSearchParams(params);
    },
  };
}

// Opens the blocked task directly. `?panel=teams-<detail-key>&detailId=` is the
// shared grammar both shells already speak (dockview's describePanel and the
// mobile page's openDetail), so one action works on desktop and mobile.
function viewIssueAction(issueId: string): { label: string; onClick: () => void } {
  return {
    label: "View",
    onClick: () => {
      capture("run_notification_clicked", { source: "interaction" });
      const params = new URLSearchParams(window.location.search);
      params.set("panel", "teams-issue-detail");
      params.set("detailId", issueId);
      pushSearchParams(params);
    },
  };
}

// A finished or failed team run opens that run: its transcript says what
// happened (the Inbox it used to land on made you hunt for it).
function viewRunAction(runId: string): { label: string; onClick: () => void } {
  return {
    label: "View",
    onClick: () => {
      capture("run_notification_clicked", { source: "teams" });
      const params = new URLSearchParams(window.location.search);
      params.set("panel", "teams-run-detail");
      params.set("detailId", runId);
      pushSearchParams(params);
    },
  };
}

// Desktop opens Activity on the approval or the agent's question itself (pinned
// under "Needs you", decided inline). The mobile-web shell has no Activity panel,
// so it keeps the Teams inbox band (an approval) or the paused task (a question).
function viewNeedsYouAction(
  action: Extract<RunToastAction, { kind: "attention" }>,
  focusId: string,
  mode: RunNotificationsProps["mode"],
): { label: string; onClick: () => void } {
  if (mode !== "desktop") return action.issueId ? viewIssueAction(action.issueId) : viewTeamsAction("needs-you");
  return {
    label: "Review",
    onClick: () => {
      capture("run_notification_clicked", { source: action.approvalId ? "approval" : "interaction" });
      openPanel("activity", { focus: focusId }, { pushUrl: true });
    },
  };
}

/**
 * "<Agent> needs your approval: <title>" / "<Agent> needs your confirmation: <task>".
 * The live event carries only the kind, so the words come from the Activity feed's
 * own rendering of the item (refetched here, which also moves the rail badge); the
 * generic line is the fallback.
 */
async function needsYouCopy(
  action: Extract<RunToastAction, { kind: "attention" }>,
  focusId: string,
  agents: Agent[],
  refreshActivity: () => Promise<ActivityItem[]>,
): Promise<{ title: string; body: string }> {
  const items = await refreshActivity().catch(() => [] as ActivityItem[]);
  const item = items.find((entry) => entry.id === focusId);
  if (item) return approvalNotificationCopy(item);
  if (!action.approvalId) return { title: "Waiting on you", body: action.message };
  const agent = action.agentId ? agents.find((a) => a.id === action.agentId) : undefined;
  return { title: `${agent ? agentDisplayName(agent) : "An agent"} needs your approval`, body: action.message };
}

export interface ExecuteContext {
  mode: RunNotificationsProps["mode"];
  refreshActivity: () => Promise<ActivityItem[]>;
  /** A GET on the backend API (`useApi().get`), for a team run's agent + task names. */
  apiGet: (path: string) => Promise<unknown>;
}

type RunAction = Extract<RunToastAction, { kind: "working" | "success" | "failure" }>;

/** "Pulse · ISOA-400" / "Finished: Draft launch posts" for a team (Paperclip) run. */
async function teamRunCopy(action: RunAction, agents: Agent[], apiGet: ExecuteContext["apiGet"]): Promise<{ title: string; body: string }> {
  const [issue, teamAgents] = await Promise.all([
    action.issueId ? apiGet(`/teams/issues/${action.issueId}`).catch(() => null) : null,
    apiGet("/teams/agents").catch(() => null),
  ]) as [{ identifier?: string | null; title?: string | null } | null, { agents?: { id: string; name?: string }[] } | null];
  const name = teamAgents?.agents?.find((agent) => agent.id === action.agentId)?.name
    ?? agents.find((agent) => agent.id === action.agentId)?.name ?? "An agent";
  const title = notificationTitle(name, issue?.identifier || null);
  if (action.kind === "failure") return { title, body: runFailureLine(action.errorCode, action.error) };
  return { title, body: notificationLine(issue?.title ? `Finished: ${issue.title}` : "Finished a run") };
}

function executeAction(action: RunToastAction, agents: Agent[], ctx: ExecuteContext): void {
  if (action.kind === "dismiss") {
    dismissNotification(action.key);
    return;
  }
  const focusId = action.kind !== "attention" ? null
    : action.approvalId ? approvalActivityId(action.approvalId)
      : action.interactionId && action.issueId ? interactionActivityId(action.issueId, action.interactionId) : null;
  if (action.kind === "attention" && focusId) {
    capture("run_notification_shown", { source: action.approvalId ? "approval" : "interaction", kind: action.kind });
    const view = viewNeedsYouAction(action, focusId, ctx.mode);
    void needsYouCopy(action, focusId, agents, ctx.refreshActivity).then(({ title, body }) =>
      nativeNotify(title, body, { onClick: view.onClick, tag: action.key }).then((shown) => {
        if (!shown) notifyAttention(title, { dedupeKey: action.key, description: body, action: view });
      }));
    return;
  }
  if (action.kind === "attention") {
    // Same discriminator the View target uses two lines down. Without it both
    // families report kind:"attention"/source:"teams" and impressions stop
    // telling approvals apart from paused tasks — while clicks still do.
    capture("run_notification_shown", {
      source: action.issueId ? "interaction" : "approval",
      kind: action.kind,
    });
    // Native notifications take a title AND a body; the specific line reads
    // better as the body under a short standing title. Native replaces the toast.
    // Land on the blocked task itself when we know it; the inbox is only the
    // fallback for approvals, which carry no issue id.
    const view = action.issueId ? viewIssueAction(action.issueId) : viewTeamsAction("needs-you");
    void nativeNotify("Waiting on you", action.message, { onClick: view.onClick, tag: action.key }).then((shown) => {
      if (shown) return;
      notifyAttention("Waiting on you", { dedupeKey: action.key, description: action.message, action: view });
    });
    return;
  }
  capture("run_notification_shown", { source: action.source, kind: action.kind });
  // No persistent "is working…" toast for any run: it sat on the composer (owner,
  // 2026-09-25), and the run is already on screen as its room's live card or in the
  // room list's working state. Team runs use the Activity-rail indicator. The
  // reducer still tracks the run so its completion notifies when unwatched.
  if (action.kind === "working") return;
  if (action.source === "teams") {
    const view = viewRunAction(action.key.replace(/^teams:/, ""));
    void teamRunCopy(action, agents, ctx.apiGet).then(({ title, body }) =>
      nativeNotify(title, body, { onClick: view.onClick, tag: action.key }).then((shown) => {
        if (shown) dismissNotification(action.key);
        else if (action.kind === "success") notifySuccess(title, { dedupeKey: action.key, description: body, action: view });
        else notifyFailure(title, { dedupeKey: action.key, description: body, action: view });
      }));
    return;
  }
  // Line 1 who · where ("Pulse · #GTM Strategy"), line 2 what it replied or why it stopped.
  const room = action.roomId ? getRoomView().rooms.get(action.roomId) : undefined;
  const title = notificationTitle(agentName(action.agentId, agents), roomWhere(room));
  const view = viewAgentAction(action.agentId, action.roomId);
  // In a DM or group chat every message notifies, so the reply (or the failure
  // note the backend posts) brings its own banner: replace, don't stack.
  if (room && room.kind !== "channel") {
    dismissNotification(action.key);
    return;
  }
  const body = action.kind === "success" ? action.line || "Replied" : runFailureLine();
  // A native banner replaces the in-app toast.
  void nativeNotify(title, body, { onClick: view.onClick, tag: action.key }).then((shown) => {
    if (shown) dismissNotification(action.key);
    else if (action.kind === "success") notifySuccess(title, { dedupeKey: action.key, description: body, action: view });
    else notifyFailure(title, { dedupeKey: action.key, description: body, action: view });
  });
}

export function RunNotifications(props: RunNotificationsProps) {
  const { onEvent, onChatMessage } = useLiveEvents();
  const { agents } = useAgents();
  const workspace = useWorkspaceState();

  const stateRef = useRef(createRunToastState());
  const activeTeamRunCount = useActiveTeamRunCount();
  const ctxRef = useRef<RunToastContext>({
    watchedChatKeys: new Set(),
    teamsPanelActive: false,
  });
  const agentsRef = useRef<Agent[]>([]);
  const { refresh: refreshActivity } = useActivity();
  const api = useApi();
  const execRef = useRef<ExecuteContext>({ mode: props.mode, refreshActivity, apiGet: api.get });
  const roomView = useSyncExternalStore(subscribeRoomView, getRoomView, getRoomView);

  // Keep the latest props/agents available to the async live-event callbacks
  // below without making them effect dependencies (that would tear down and
  // resubscribe the chat/event listeners on every keystroke-adjacent
  // re-render). Writing refs during render is disallowed (react-hooks/refs),
  // so the sync happens in an unconditional effect instead — it still lands
  // before any queued live-event callback runs.
  useEffect(() => {
    ctxRef.current =
      props.mode === "mobile"
        ? {
            watchedChatKeys:
              props.activeView === "chat" && props.selectedAgentId
                ? new Set([props.selectedAgentId])
                : new Set(),
            teamsPanelActive:
              props.activeView === "control" && props.activePanel.startsWith("teams-"),
          }
        : {
            // Desktop: every visible chat panel contributes a bare (agent-level)
            // key plus the compound key for the session it is showing.
            watchedChatKeys: buildDesktopWatchedKeys(workspace.visibleChatAgentIds),
            // A teams panel counts as active only while it is the ACTIVE tab
            // of some dockview group — open-but-background tabs don't
            // suppress. See the teamsPanelVisible note in workspaceController.
            teamsPanelActive: workspace.teamsPanelVisible,
          };
    ctxRef.current = { ...ctxRef.current, visibleRoomId: roomView.visibleRoomId };
    agentsRef.current = agents;
    execRef.current = { mode: props.mode, refreshActivity, apiGet: api.get };
  });

  useEffect(() => {
    const unsubChat = onChatMessage((msg, event) => {
      try {
        for (const action of reduceChatFrame(
          stateRef.current,
          msg,
          ctxRef.current,
          { runId: event.payload.runId, agentId: event.payload.agentId, roomId: event.payload.roomId },
        )) {
          executeAction(action, agentsRef.current, execRef.current);
        }
      } catch {
        // Never let a notification bug interfere with chat delivery.
      }
    });
    const unsubEvent = onEvent((event) => {
      if (event.type !== "paperclip.event") return;
      try {
        trackTeamRunActivity(event.payload.eventName, event.payload.payload);
        const actions = [
          ...reduceTeamsRunEvent(
            stateRef.current,
            event.payload.eventName,
            event.payload.payload,
            ctxRef.current,
          ),
          ...reduceTeamsActivityEvent(
            event.payload.eventName,
            event.payload.payload,
            ctxRef.current,
          ),
        ];
        for (const action of actions) {
          executeAction(action, agentsRef.current, execRef.current);
        }
      } catch {
        // Never let a notification bug interfere with event delivery.
      }
    });
    return () => {
      unsubChat();
      unsubEvent();
    };
  }, [onChatMessage, onEvent]);

  if (props.mode !== "mobile" || activeTeamRunCount === 0) return null;
  const label = activeTeamRunCount === 1 ? "Team is working" : `${activeTeamRunCount} team runs active`;
  return <button type="button" className="team-run-mobile-indicator" onClick={viewTeamsAction("recently").onClick}
    aria-label={`${label}. View activity`}>
    <LoaderCircle size={14} aria-hidden="true" />
    <span>{label}</span>
    <span className="team-run-mobile-indicator__view" aria-hidden="true">View</span>
  </button>;
}
