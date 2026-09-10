"use client";

import { useEffect, useRef } from "react";
import type { IDockviewPanelProps } from "dockview-react";
import { MessagingConversation } from "@/components/messaging/MessagingWorkspace";
import { useMessagingWorkspace } from "@/components/messaging/MessagingWorkspaceContext";
import { useAgents } from "@/hooks/useAgents";
import { useDockPanelParams } from "./useDockPanelParams";
import { DEFAULT_AGENT_SENTINEL } from "../panelRegistry";
import { syncMessagesPanelAgent } from "../workspaceController";

interface ChatPanelParams {
  agentId?: string;
  agentSelection?: "resolved";
}

/**
 * Agent parameters are one-shot selection intents for the shared conversation.
 * Room changes must not snap back to the last agent requested by a deep link.
 */
export function ChatPanel(props: IDockviewPanelProps<ChatPanelParams>): React.ReactElement {
  const params = useDockPanelParams(props);
  const { defaultId } = useAgents();
  const { selectAgentDm, roomProps } = useMessagingWorkspace();
  const lastRequested = useRef<string | null>(null);
  const requested = params.agentSelection === "resolved" ? null : params.agentId === DEFAULT_AGENT_SENTINEL
    ? defaultId ?? null
    : params.agentId ?? null;

  useEffect(() => {
    if (!requested) {
      lastRequested.current = null;
      return;
    }
    if (lastRequested.current === requested) return;
    lastRequested.current = requested;
    void selectAgentDm(requested).catch(() => {
      if (lastRequested.current === requested) lastRequested.current = null;
    });
  }, [requested, selectAgentDm]);

  const roomId = roomProps?.roomId;
  const roomAgentId = roomProps?.composer.focusAgentId;
  const waitingForDefault = params.agentSelection !== "resolved"
    && params.agentId === DEFAULT_AGENT_SENTINEL && !defaultId;
  useEffect(() => {
    // The initial fallback room must not consume a still-unresolved deep link.
    if (roomId && !waitingForDefault) syncMessagesPanelAgent(roomAgentId);
  }, [roomId, roomAgentId, params.agentId, params.agentSelection, waitingForDefault]);

  return <MessagingConversation />;
}
