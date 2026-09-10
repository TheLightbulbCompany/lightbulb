"use client";

import { useEffect, useRef } from "react";
import type { IDockviewPanelProps } from "dockview-react";
import { MessagingConversation } from "@/components/messaging/MessagingWorkspace";
import { useMessagingWorkspace } from "@/components/messaging/MessagingWorkspaceContext";
import { useAgents } from "@/hooks/useAgents";
import { useDockPanelParams } from "./useDockPanelParams";
import { DEFAULT_AGENT_SENTINEL } from "../panelRegistry";

interface ChatPanelParams {
  agentId?: string;
}

/**
 * Agent parameters are one-shot selection intents for the shared conversation.
 * Room changes must not snap back to the last agent requested by a deep link.
 */
export function ChatPanel(props: IDockviewPanelProps<ChatPanelParams>): React.ReactElement {
  const params = useDockPanelParams(props);
  const { defaultId } = useAgents();
  const { selectAgentDm } = useMessagingWorkspace();
  const lastRequested = useRef<string | null>(null);
  const requested = params.agentId === DEFAULT_AGENT_SENTINEL
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

  return <MessagingConversation />;
}
