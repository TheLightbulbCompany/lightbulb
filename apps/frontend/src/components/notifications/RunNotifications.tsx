"use client";

import { useEffect, useRef } from "react";
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

function viewAgentAction(agentId: string): { label: string; onClick: () => void } {
  return {
    label: "View",
    onClick: () => {
      capture("run_notification_clicked", { source: "chat" });
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
    notifyAttention(action.message, {
      dedupeKey: action.key,
      // Land on the blocked task itself when we know it; the inbox is only the
      // fallback for approvals, which carry no issue id.
      action: action.issueId ? viewIssueAction(action.issueId) : viewTeamsAction("needs-you"),
    });
    // Native notifications take a title AND a body; the specific line reads
    // better as the body under a short standing title.
    nativeNotify("Waiting on you", action.message);
    return;
  }
  const isChat = action.source === "chat";
  const name = isChat ? agentName(action.agentId, agents) : null;
  const view = isChat ? viewAgentAction(action.agentId) : viewTeamsAction("recently");
  capture("run_notification_shown", { source: action.source, kind: action.kind });
  if (action.kind === "working") {
    notifyLoading(isChat ? `${name} is working…` : "A team agent run started…", {
      dedupeKey: action.key,
      action: view,
    });
  } else if (action.kind === "success") {
    const message = isChat ? `${name} finished a run` : "Team agent run completed";
    notifySuccess(message, { dedupeKey: action.key, action: view });
    nativeNotify(message);
  } else {
    const message = isChat ? `${name} run failed` : "Team agent run failed";
    notifyFailure(message, { dedupeKey: action.key, action: view });
    nativeNotify(message);
  }
}

export function RunNotifications(props: RunNotificationsProps): null {
  const { onEvent, onChatMessage } = useLiveEvents();
  const { agents } = useAgents();
  const workspace = useWorkspaceState();

  const stateRef = useRef(createRunToastState());
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
          { runId: event.payload.runId, agentId: event.payload.agentId },
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

  return null;
}
