"use client";

import { useEffect, useRef } from "react";
import { LoaderCircle } from "lucide-react";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { useAgents, agentDisplayName, type Agent } from "@/hooks/useAgents";
import { useWorkspaceState } from "@/components/workspace/workspaceController";
import { capture } from "@/lib/analytics";
import {
  notifyLoading,
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

function executeAction(action: RunToastAction, agents: Agent[]): void {
  if (action.kind === "dismiss") {
    dismissNotification(action.key);
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
    void nativeNotify("Waiting on you", action.message).then((shown) => {
      if (shown) return;
      notifyAttention(action.message, {
        dedupeKey: action.key,
        // Land on the blocked task itself when we know it; the inbox is only the
        // fallback for approvals, which carry no issue id.
        action: action.issueId ? viewIssueAction(action.issueId) : viewTeamsAction("needs-you"),
      });
    });
    return;
  }
  const isChat = action.source === "chat";
  const name = isChat ? agentName(action.agentId, agents) : null;
  const view = isChat ? viewAgentAction(action.agentId, action.roomId) : viewTeamsAction("recently");
  capture("run_notification_shown", { source: action.source, kind: action.kind });
  if (action.kind === "working") {
    // Background team runs use the persistent Activity-rail indicator instead
    // of an infinite bottom-right toast that obscures the composer and roster.
    if (!isChat) return;
    notifyLoading(`${name} is working…`, {
      dedupeKey: action.key,
      action: view,
    });
  } else if (action.kind === "success") {
    const message = isChat ? `${name} finished a run` : "Team agent run completed";
    // A native completion replaces the toast, so clear the "working…" one it
    // would otherwise have replaced in place.
    void nativeNotify(message).then((shown) => {
      if (shown) dismissNotification(action.key);
      else notifySuccess(message, { dedupeKey: action.key, action: view });
    });
  } else {
    const message = isChat ? `${name} run failed` : "Team agent run failed";
    void nativeNotify(message, undefined, { onClick: view.onClick, tag: action.key }).then((shown) => {
      if (shown) dismissNotification(action.key);
      else notifyFailure(message, { dedupeKey: action.key, action: view });
    });
  }
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
    agentsRef.current = agents;
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
          executeAction(action, agentsRef.current);
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
          executeAction(action, agentsRef.current);
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
