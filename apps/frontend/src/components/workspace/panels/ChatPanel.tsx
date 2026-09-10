"use client";

import { useEffect, useMemo } from "react";
import type { IDockviewPanelProps } from "dockview-react";
import { MessagingWorkspace } from "@/components/messaging/MessagingWorkspace";
import { useAgents, agentDisplayName } from "@/hooks/useAgents";
import { DEFAULT_AGENT_SENTINEL } from "../panelRegistry";

interface ChatPanelParams {
  agentId: string;
}

/**
 * The historical chat panel ID remains a durable dock/deep-link identifier, but
 * its content is now the canonical Lightbulb room surface. An agent deep link
 * selects that agent's DM; the room directory remains available beside it.
 */
export function ChatPanel(props: IDockviewPanelProps<ChatPanelParams>): React.ReactElement {
  const { agents, defaultId } = useAgents();
  const requested = props.params.agentId;
  const initialAgentId = requested === DEFAULT_AGENT_SENTINEL ? defaultId ?? null : requested;
  const title = useMemo(() => {
    const agent = agents.find((candidate) => candidate.id === initialAgentId);
    return agent ? agentDisplayName(agent) : "Messages";
  }, [agents, initialAgentId]);

  useEffect(() => {
    if (props.api.title !== title) props.api.setTitle(title);
  }, [props.api, title]);

  return <MessagingWorkspace initialAgentId={initialAgentId} />;
}
