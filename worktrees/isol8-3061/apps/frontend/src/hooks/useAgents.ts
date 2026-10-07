"use client";

import {
  agentDisplayName,
  getAgentModelString,
  useAgents as useAgentsShared,
  type Agent,
  type AgentModelRef,
} from "@lightbulb/shared";
import { capture } from "@/lib/analytics";
import { useLiveEvents } from "@/hooks/useLiveEvents";

// Agent types + pure helpers live in @lightbulb/shared. This wrapper binds the
// web runtime source and PostHog so feature code keeps its no-arg signature.
export { agentDisplayName, getAgentModelString };
export type { Agent, AgentModelRef };

export function useAgents() {
  return useAgentsShared(useLiveEvents(), { capture });
}
