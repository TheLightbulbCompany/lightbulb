"use client";

import { useEffect, useRef, useState } from "react";
import type { IDockviewPanelProps } from "dockview-react";
import { MessagingConversation } from "@/components/messaging/MessagingWorkspace";
import { useMessagingWorkspace } from "@/components/messaging/MessagingWorkspaceContext";
import { useAgents } from "@/hooks/useAgents";
import { useDockPanelParams } from "./useDockPanelParams";
import { DEFAULT_AGENT_SENTINEL } from "../panelRegistry";
import { syncMessagesPanelAgent } from "../workspaceController";
import { releaseFocusComposerRequest } from "@/lib/focusComposerRequest";

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
  const navigationRequest = useRef(0);
  const [focusDestination, setFocusDestination] = useState<{ agentId: string; roomId: string } | null>(null);
  const deliveredDestination = useRef<typeof focusDestination>(null);
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
    const request = ++navigationRequest.current;
    void selectAgentDm(requested).then((roomId) => {
      if (roomId && navigationRequest.current === request) setFocusDestination({ agentId: requested, roomId });
    }).catch(() => {
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

  useEffect(() => {
    if (!focusDestination || focusDestination === deliveredDestination.current || roomId !== focusDestination.roomId) return;
    deliveredDestination.current = focusDestination;
    if (releaseFocusComposerRequest(focusDestination.agentId)) {
      window.dispatchEvent(new CustomEvent("getstarted:focus-composer", {
        detail: { agentId: focusDestination.agentId },
      }));
    }
  }, [focusDestination, roomId]);

  return <MessagingConversation />;
}
