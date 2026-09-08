"use client";

import { useEffect, useRef } from "react";
import { useGateway } from "@/hooks/useGateway";
import { useActiveSession, getActiveSessionId } from "@/hooks/useActiveSession";
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
  acquireTeamsEvents,
  releaseTeamsEvents,
  resubscribeTeamsEvents,
} from "@/lib/teamsSubscription";
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
 * unfiltered chat-frame stream (background webchat runs already reach this
 * browser and are dropped by useAgentChat's demux) plus teams.heartbeat.run.*
 * events, and drives one live toast per run keyed by runId.
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

// Desktop watched-key set: for each visible chat panel's agent, the bare
// (agent-level) key AND the `${agentId}:${session}` key for the session that
// panel is showing. getActiveSessionId is a non-hook module read (the session
// per agent can't be resolved with a hook in a loop); null == General maps to
// the trailing-colon key, matching chatSuppressed's `?? ""` normalization.
function buildDesktopWatchedKeys(visibleChatAgentIds: string[]): Set<string> {
  const keys = new Set<string>();
  for (const agentId of visibleChatAgentIds) {
    keys.add(agentId);
    keys.add(`${agentId}:${getActiveSessionId(agentId) ?? ""}`);
  }
  return keys;
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
  const { isConnected, send, onEvent, onChatMessage } = useGateway();
  const { agents } = useAgents();
  // Both hooks run unconditionally every render (rules-of-hooks) regardless of
  // mode. In desktop mode `useActiveSession(null)` is inert (returns null); in
  // mobile mode `useWorkspaceState()` returns the empty snapshot (no dockview
  // attached) and is simply unused.
  const mobileAgentId = props.mode === "mobile" ? props.selectedAgentId : null;
  const { activeSessionId } = useActiveSession(mobileAgentId);
  const workspace = useWorkspaceState();

  const stateRef = useRef(createRunToastState());
  const ctxRef = useRef<RunToastContext>({
    watchedChatKeys: new Set(),
    teamsPanelActive: false,
  });
  const agentsRef = useRef<Agent[]>([]);

  // Keep the latest props/agents available to the async gateway callbacks
  // below without making them effect dependencies (that would tear down and
  // resubscribe the chat/event listeners on every keystroke-adjacent
  // re-render). Writing refs during render is disallowed (react-hooks/refs),
  // so the sync happens in an unconditional effect instead — it still lands
  // before any queued gateway callback runs.
  useEffect(() => {
    ctxRef.current =
      props.mode === "mobile"
        ? {
            // Compound-only, per-session (byte-equivalent to the old logic):
            // one key when chat is the active view and an agent is selected.
            watchedChatKeys:
              props.activeView === "chat" && props.selectedAgentId
                ? new Set([`${props.selectedAgentId}:${activeSessionId ?? ""}`])
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

  // Ordering contract (see teamsSubscription): acquire/release MUST be
  // declared before the resubscribe effect so the refcount is ≥ 1 when the
  // mount-while-connected resubscribe fires.
  useEffect(() => {
    acquireTeamsEvents();
    return () => releaseTeamsEvents(send);
  }, [send]);

  useEffect(() => {
    if (isConnected) resubscribeTeamsEvents(send);
  }, [isConnected, send]);

  useEffect(() => {
    const unsubChat = onChatMessage((msg) => {
      try {
        for (const action of reduceChatFrame(stateRef.current, msg, ctxRef.current)) {
          executeAction(action, agentsRef.current);
        }
      } catch {
        // Never let a notification bug interfere with chat delivery.
      }
    });
    const unsubEvent = onEvent((event, payload) => {
      try {
        const actions = [
          ...reduceTeamsRunEvent(stateRef.current, event, payload, ctxRef.current),
          ...reduceTeamsActivityEvent(event, payload, ctxRef.current),
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
