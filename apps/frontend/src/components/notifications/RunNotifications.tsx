"use client";

import { useEffect, useRef } from "react";
import { useGateway } from "@/hooks/useGateway";
import { useAgents, agentDisplayName, type Agent } from "@/hooks/useAgents";
import { capture } from "@/lib/analytics";
import {
  notifyLoading,
  notifySuccess,
  notifyFailure,
  dismissNotification,
} from "@/lib/notify";
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
export interface RunNotificationsProps {
  selectedAgentId: string | null;
  activeView: "chat" | "control";
  activePanel: string;
}

function agentName(agentId: string, agents: Agent[]): string {
  const agent = agents.find((a) => a.id === agentId);
  return agent ? agentDisplayName(agent) : "Agent";
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

function viewTeamsAction(): { label: string; onClick: () => void } {
  return {
    label: "View",
    onClick: () => {
      capture("run_notification_clicked", { source: "teams" });
      const params = new URLSearchParams(window.location.search);
      params.set("panel", "teams-activity");
      params.delete("detailId");
      pushSearchParams(params);
    },
  };
}

function executeAction(action: RunToastAction, agents: Agent[]): void {
  if (action.kind === "dismiss") {
    dismissNotification(action.key);
    return;
  }
  const isChat = action.source === "chat";
  const name = isChat ? agentName(action.agentId, agents) : null;
  const view = isChat ? viewAgentAction(action.agentId) : viewTeamsAction();
  capture("run_notification_shown", { source: action.source, kind: action.kind });
  if (action.kind === "working") {
    notifyLoading(isChat ? `${name} is working…` : "A team agent run started…", {
      dedupeKey: action.key,
      action: view,
    });
  } else if (action.kind === "success") {
    notifySuccess(isChat ? `${name} finished a run` : "Team agent run completed", {
      dedupeKey: action.key,
      action: view,
    });
  } else {
    notifyFailure(isChat ? `${name} run failed` : "Team agent run failed", {
      dedupeKey: action.key,
      action: view,
    });
  }
}

export function RunNotifications({
  selectedAgentId,
  activeView,
  activePanel,
}: RunNotificationsProps): null {
  const { isConnected, send, onEvent, onChatMessage } = useGateway();
  const { agents } = useAgents();

  const stateRef = useRef(createRunToastState());
  const ctxRef = useRef<RunToastContext>({
    selectedAgentId: null,
    chatViewActive: true,
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
    ctxRef.current = {
      selectedAgentId,
      chatViewActive: activeView === "chat",
      teamsPanelActive: activeView === "control" && activePanel.startsWith("teams-"),
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
        for (const action of reduceTeamsRunEvent(stateRef.current, event, payload, ctxRef.current)) {
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
