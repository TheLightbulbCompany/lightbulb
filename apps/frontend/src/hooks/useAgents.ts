"use client";

import {
  agentDisplayName,
  getAgentModelString,
  useAgents as useAgentsShared,
  type Agent,
  type AgentModelRef,
} from "@isol8/shared";
import { capture } from "@/lib/analytics";
import { useGateway } from "@/hooks/useGateway";

// Agent types + pure helpers moved to @isol8/shared (Phase 1 extraction);
// re-exported so feature code keeps importing them from here. The hook body
// followed them once mobile needed the same roster hardening (the empty-roster
// latch + the become-reachable revalidation) — this wrapper binds the web
// gateway context and PostHog so feature code keeps its no-arg signature.
export { agentDisplayName, getAgentModelString };
export type { Agent, AgentModelRef };

export function useAgents() {
  return useAgentsShared(useGateway(), { capture });
}
